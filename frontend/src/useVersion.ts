// useVersion.ts — fetch the running version + update hint once, share it across
// the nav footer and Settings (D-0053). The value changes only on deploy, so a
// module-level cache means both consumers ride a single request per app load.
import { useEffect, useState } from "react";
import { api } from "./api";
import type { VersionInfo } from "./types";

let cache: VersionInfo | null = null;
let inflight: Promise<VersionInfo> | null = null;

function load(): Promise<VersionInfo> {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = api
      .getVersion()
      .then((v) => {
        cache = v;
        return v;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export function useVersion(): VersionInfo | null {
  const [info, setInfo] = useState<VersionInfo | null>(cache);
  useEffect(() => {
    let alive = true;
    // Best-effort: a failed version fetch must never disrupt the UI.
    load()
      .then((v) => alive && setInfo(v))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  return info;
}
