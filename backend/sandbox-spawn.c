/*
 * sandbox-spawn.c — minimal setuid privilege-drop helper for fs-isolation.
 *
 * The backend runs as the non-root control-plane user `batond` and must launch
 * agent CLIs as the low-privilege `sandbox` user so kernel DAC fences the agent
 * off from /app and control-plane /data (it cannot read files it does not own).
 * A non-root parent cannot setuid() to another user, so this tiny helper is the
 * single privileged surface: installed setuid-root, mode 4750 root:batond, so
 * ONLY members of the `batond` group (the backend) may exec it.
 *
 * Two modes:
 *   spawn:  sandbox-spawn -- <cmd> [args...]
 *           Drop to `sandbox` (primary group `sandbox`, supplementary incl.
 *           `agents` for shared workspace write), set umask 002, reset
 *           HOME/USER/LOGNAME from passwd, then execvp the command. A clean
 *           execve with NO intermediate fork — preserves the controlling tty the
 *           parent set up (start_new_session + slave fd), which the web-TTY PTY
 *           seam depends on.
 *
 *   reap:   sandbox-spawn --reap <pgid>
 *           kill(-pgid, SIGKILL) as root. The web-TTY reaper: batond cannot
 *           signal sandbox-owned processes cross-user, so PtySession.close()
 *           routes the kill through here. Only batond-group can invoke it.
 *
 * Keep this small and auditable. No shell, no env interpolation, no path search
 * beyond execvp's PATH (cmd is supplied by the trusted backend, not the agent).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pwd.h>
#include <grp.h>
#include <signal.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/types.h>
#include <sys/stat.h>

#define SANDBOX_USER "sandbox"

/* ── Landlock path jail (P-0072) ──────────────────────────────────────────────
 *
 * Every session's agent runs as the SAME uid (`sandbox`), and session workspaces
 * are deliberately group-co-writable so batond can git-init/commit them. POSIX
 * DAC therefore cannot separate one session from another — which is how a pilot
 * agent came to write its entire output into a different session's worktree.
 *
 * Landlock is the fence: a path-based LSM ruleset the process applies TO ITSELF
 * before exec. It grants no privilege — it only ever removes access — so this
 * does not widen the setuid surface. Requires Linux >= 5.13 with Landlock
 * enabled; the caller probes support via --jail-probe and decides policy, so
 * this file's rule is simple: if --jail was asked for and cannot be applied,
 * REFUSE. A jail that silently didn't happen is worse than no jail.
 *
 * Guarded by __has_include so the helper still builds on hosts without the
 * Landlock uapi header (it then refuses --jail rather than pretending).
 */
#if defined(__linux__) && defined(__has_include)
#  if __has_include(<linux/landlock.h>)
#    define BK_HAVE_LANDLOCK 1
#  endif
#endif

#ifdef BK_HAVE_LANDLOCK
#include <linux/landlock.h>
#include <sys/prctl.h>
#include <sys/syscall.h>

/* Syscall numbers are architecture-generic (assigned in 5.13); define them when
 * building against pre-5.13 asm headers. */
#ifndef __NR_landlock_create_ruleset
#define __NR_landlock_create_ruleset 444
#endif
#ifndef __NR_landlock_add_rule
#define __NR_landlock_add_rule 445
#endif
#ifndef __NR_landlock_restrict_self
#define __NR_landlock_restrict_self 446
#endif

/* Access bits added after ABI v1 — defined here so an older uapi header still
 * compiles; each is only ever used when the running kernel's ABI advertises it. */
#ifndef LANDLOCK_ACCESS_FS_REFER
#define LANDLOCK_ACCESS_FS_REFER (1ULL << 13)
#endif
#ifndef LANDLOCK_ACCESS_FS_TRUNCATE
#define LANDLOCK_ACCESS_FS_TRUNCATE (1ULL << 14)
#endif
#ifndef LANDLOCK_ACCESS_FS_IOCTL_DEV
#define LANDLOCK_ACCESS_FS_IOCTL_DEV (1ULL << 15)
#endif

#define BK_ACCESS_READ ( \
    LANDLOCK_ACCESS_FS_READ_FILE | \
    LANDLOCK_ACCESS_FS_READ_DIR | \
    LANDLOCK_ACCESS_FS_EXECUTE)

#define BK_ACCESS_WRITE_V1 ( \
    LANDLOCK_ACCESS_FS_WRITE_FILE | \
    LANDLOCK_ACCESS_FS_REMOVE_DIR | \
    LANDLOCK_ACCESS_FS_REMOVE_FILE | \
    LANDLOCK_ACCESS_FS_MAKE_CHAR | \
    LANDLOCK_ACCESS_FS_MAKE_DIR | \
    LANDLOCK_ACCESS_FS_MAKE_REG | \
    LANDLOCK_ACCESS_FS_MAKE_SOCK | \
    LANDLOCK_ACCESS_FS_MAKE_FIFO | \
    LANDLOCK_ACCESS_FS_MAKE_BLOCK | \
    LANDLOCK_ACCESS_FS_MAKE_SYM)

/* Readable/executable, never writable. Deliberately excludes /data (other
 * sessions' workspaces, the evidence store, the DB), /app (control plane), /work
 * and /home — reads of another session's tree are part of the defect, not just
 * writes, so the confidential case is covered too. Missing entries are skipped:
 * these vary by base image. */
static const char *const RO_PATHS[] = {
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
    "/etc", "/opt", "/proc", "/sys", "/run", "/var", NULL
};

/* Writable in addition to the jail and the agent's own HOME. /dev is needed for
 * the PTY seam and the usual null/urandom; /tmp for toolchain scratch. */
static const char *const RW_PATHS[] = { "/tmp", "/dev", NULL };

static int ll_create_ruleset(const struct landlock_ruleset_attr *attr,
                             size_t size, __u32 flags) {
    return (int)syscall(__NR_landlock_create_ruleset, attr, size, flags);
}

static int ll_add_rule(int fd, enum landlock_rule_type type,
                       const void *attr, __u32 flags) {
    return (int)syscall(__NR_landlock_add_rule, fd, type, attr, flags);
}

static int ll_restrict_self(int fd, __u32 flags) {
    return (int)syscall(__NR_landlock_restrict_self, fd, flags);
}

/* The kernel's Landlock ABI version, or -1 when unsupported/disabled. */
static int landlock_abi(void) {
    int abi = ll_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    return abi < 1 ? -1 : abi;
}

/* Grant `access` on `path`. A missing path is not an error — the RO list spans
 * several base-image layouts. Returns 0 on success or skip, -1 on real failure. */
static int allow_path(int ruleset_fd, const char *path, __u64 access) {
    struct landlock_path_beneath_attr rule = { .allowed_access = access };
    rule.parent_fd = open(path, O_PATH | O_CLOEXEC);
    if (rule.parent_fd < 0) {
        if (errno == ENOENT)
            return 0;
        fprintf(stderr, "sandbox-spawn: open('%s'): %s\n", path, strerror(errno));
        return -1;
    }
    int rc = ll_add_rule(ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, &rule, 0);
    int saved = errno;
    close(rule.parent_fd);
    if (rc != 0) {
        fprintf(stderr, "sandbox-spawn: add_rule('%s'): %s\n", path, strerror(saved));
        return -1;
    }
    return 0;
}

/* Confine this process to `jail` (read-write) plus a read-only system view and a
 * writable HOME. Must be called AFTER the privilege drop and immediately before
 * execvp — the restriction is inherited across exec and cannot be undone. */
static int apply_jail(const char *jail, const char *home) {
    errno = 0;
    int abi = landlock_abi();
    if (abi < 0) {
        fprintf(stderr, "sandbox-spawn: landlock unavailable (%s) — needs Linux "
                        ">= 5.13 with landlock enabled\n", strerror(errno));
        return -1;
    }

    __u64 rw = BK_ACCESS_READ | BK_ACCESS_WRITE_V1;
    if (abi >= 2) rw |= LANDLOCK_ACCESS_FS_REFER;      /* cross-dir rename/link */
    if (abi >= 3) rw |= LANDLOCK_ACCESS_FS_TRUNCATE;   /* O_TRUNC writes */
    if (abi >= 5) rw |= LANDLOCK_ACCESS_FS_IOCTL_DEV;  /* tty ioctls on /dev */

    /* Everything the ruleset governs. Anything in `handled` that a path is not
     * granted is denied there — that is the whole fence. */
    struct landlock_ruleset_attr attr = { .handled_access_fs = rw };
    int fd = ll_create_ruleset(&attr, sizeof(attr), 0);
    if (fd < 0) {
        fprintf(stderr, "sandbox-spawn: create_ruleset: %s\n", strerror(errno));
        return -1;
    }

    int rc = 0;
    for (int i = 0; RO_PATHS[i] && rc == 0; i++)
        rc = allow_path(fd, RO_PATHS[i], BK_ACCESS_READ & rw);
    for (int i = 0; RW_PATHS[i] && rc == 0; i++)
        rc = allow_path(fd, RW_PATHS[i], rw);
    /* The agent's HOME holds CLI OAuth/config and is shared by every session by
     * design (it is credentials, not work product) — the shared writable surface
     * shrinks to this, it does not disappear. */
    if (rc == 0 && home && *home)
        rc = allow_path(fd, home, rw);
    /* The jail itself, last so its failure is unambiguous. */
    if (rc == 0) {
        struct landlock_path_beneath_attr j = { .allowed_access = rw };
        j.parent_fd = open(jail, O_PATH | O_CLOEXEC);
        if (j.parent_fd < 0) {
            fprintf(stderr, "sandbox-spawn: jail '%s': %s\n", jail, strerror(errno));
            rc = -1;
        } else {
            rc = ll_add_rule(fd, LANDLOCK_RULE_PATH_BENEATH, &j, 0);
            if (rc != 0)
                fprintf(stderr, "sandbox-spawn: add_rule(jail): %s\n", strerror(errno));
            close(j.parent_fd);
        }
    }

    if (rc == 0 && prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        fprintf(stderr, "sandbox-spawn: no_new_privs: %s\n", strerror(errno));
        rc = -1;
    }
    if (rc == 0 && ll_restrict_self(fd, 0) != 0) {
        fprintf(stderr, "sandbox-spawn: restrict_self: %s\n", strerror(errno));
        rc = -1;
    }
    close(fd);
    return rc;
}

/* Exit 0 = jail enforceable · 1 = compiled in but this kernel can't · 2 = not
 * compiled in at all. The image build asserts != 2, so a base-image change that
 * drops the Landlock headers fails the build instead of silently shipping an
 * unfenced helper. */
static int do_jail_probe(void) {
    errno = 0;
    int abi = landlock_abi();
    if (abi < 0) {
        /* errno separates the two very different causes an operator would
         * otherwise have to diagnose by hand: ENOSYS = this kernel has no
         * Landlock at all (e.g. Docker Desktop's LinuxKit VM); EPERM/EACCES =
         * present but blocked, typically by a seccomp profile. */
        const char *why =
            errno == ENOSYS ? "kernel has no landlock support"
          : (errno == EPERM || errno == EACCES) ? "blocked (seccomp profile?)"
          : strerror(errno);
        fprintf(stderr, "landlock: compiled in, unavailable — %s\n", why);
        return 1;
    }
    printf("landlock: abi %d\n", abi);
    return 0;
}
#else  /* !BK_HAVE_LANDLOCK */
static int apply_jail(const char *jail, const char *home) {
    (void)jail; (void)home;
    fprintf(stderr, "sandbox-spawn: built without landlock support\n");
    return -1;
}
static int do_jail_probe(void) {
    fprintf(stderr, "landlock: not compiled in\n");
    return 2;
}
#endif

static int do_reap(const char *pgid_str) {
    char *end = NULL;
    long pgid = strtol(pgid_str, &end, 10);
    if (end == pgid_str || *end != '\0' || pgid <= 1) {
        fprintf(stderr, "sandbox-spawn: invalid pgid '%s'\n", pgid_str);
        return 2;
    }
    /* Running as root (setuid): can signal the sandbox-owned process group. */
    if (kill((pid_t)-pgid, SIGKILL) != 0 && errno != ESRCH) {
        fprintf(stderr, "sandbox-spawn: kill(-%ld): %s\n", pgid, strerror(errno));
        return 1;
    }
    return 0;
}

static int do_spawn(char **argv, const char *jail) {
    struct passwd *pw = getpwnam(SANDBOX_USER);
    if (!pw) {
        fprintf(stderr, "sandbox-spawn: no such user '%s'\n", SANDBOX_USER);
        return 3;
    }

    /* Supplementary groups (incl. `agents`) BEFORE dropping uid. */
    if (initgroups(SANDBOX_USER, pw->pw_gid) != 0) {
        fprintf(stderr, "sandbox-spawn: initgroups: %s\n", strerror(errno));
        return 4;
    }
    /* gid before uid — once uid is dropped we can no longer change gids. */
    if (setgid(pw->pw_gid) != 0) {
        fprintf(stderr, "sandbox-spawn: setgid: %s\n", strerror(errno));
        return 4;
    }
    if (setuid(pw->pw_uid) != 0) {
        fprintf(stderr, "sandbox-spawn: setuid: %s\n", strerror(errno));
        return 4;
    }
    /* Defence in depth: confirm privileges cannot be restored. */
    if (setuid(0) == 0) {
        fprintf(stderr, "sandbox-spawn: refusing to run — uid still restorable\n");
        return 5;
    }

    /* Group-writable umask so shared `agents`-group workspaces stay writable by
     * both batond and sandbox (session git tree, restore, etc.). */
    umask(002);

    /* Point the child at sandbox's identity so CLIs find their own auth/config. */
    setenv("HOME", pw->pw_dir, 1);
    setenv("USER", SANDBOX_USER, 1);
    setenv("LOGNAME", SANDBOX_USER, 1);

    /* Last thing before exec: confine the filesystem view. Applied AFTER the
     * privilege drop (a Landlock ruleset is inherited across execve and cannot be
     * lifted) and only when asked for — the caller owns the policy of whether an
     * unjailable host may still run. Refuses rather than exec'ing unconfined. */
    if (jail && apply_jail(jail, pw->pw_dir) != 0) {
        fprintf(stderr, "sandbox-spawn: refusing to exec unjailed\n");
        return 6;
    }

    execvp(argv[0], argv);
    fprintf(stderr, "sandbox-spawn: exec '%s': %s\n", argv[0], strerror(errno));
    return 127;
}

int main(int argc, char **argv) {
    if (argc >= 3 && strcmp(argv[1], "--reap") == 0) {
        return do_reap(argv[2]);
    }
    if (argc == 2 && strcmp(argv[1], "--jail-probe") == 0) {
        return do_jail_probe();
    }
    /* sandbox-spawn [--jail <dir>] -- <cmd> [args...] */
    if (argc >= 5 && strcmp(argv[1], "--jail") == 0 && strcmp(argv[3], "--") == 0) {
        if (argv[2][0] != '/') {
            fprintf(stderr, "sandbox-spawn: --jail must be an absolute path\n");
            return 2;
        }
        return do_spawn(&argv[4], argv[2]);
    }
    if (argc >= 3 && strcmp(argv[1], "--") == 0) {
        return do_spawn(&argv[2], NULL);
    }
    fprintf(stderr,
            "usage: sandbox-spawn [--jail <dir>] -- <cmd> [args...]\n"
            "       sandbox-spawn --reap <pgid>\n"
            "       sandbox-spawn --jail-probe\n");
    return 2;
}
