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
#include <sys/types.h>
#include <sys/stat.h>

#define SANDBOX_USER "sandbox"

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

static int do_spawn(char **argv) {
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

    execvp(argv[0], argv);
    fprintf(stderr, "sandbox-spawn: exec '%s': %s\n", argv[0], strerror(errno));
    return 127;
}

int main(int argc, char **argv) {
    if (argc >= 3 && strcmp(argv[1], "--reap") == 0) {
        return do_reap(argv[2]);
    }
    if (argc >= 3 && strcmp(argv[1], "--") == 0) {
        return do_spawn(&argv[2]);
    }
    fprintf(stderr,
            "usage: sandbox-spawn -- <cmd> [args...]\n"
            "       sandbox-spawn --reap <pgid>\n");
    return 2;
}
