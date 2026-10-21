// Persistent browser profile (device trust), encrypted at rest with age.
// On disk there is only <state>/profile.tar.age; it is decrypted into the container's tmpfs before the run and
// re-encrypted (atomically) after a successful login. The identity (private key) comes from the run's secret
// dir; the recipient (public key) is derived from it. Caches are not saved.
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, readFile, rename, rm } from "node:fs/promises";

const EXCLUDES = [
  "./Default/Cache", "./Default/Code Cache", "./Default/GPUCache", "./Default/Service Worker/CacheStorage",
  "./Default/DawnCache", "./Default/DawnGraphiteCache", "./GrShaderCache", "./ShaderCache", "./GraphiteDawnCache",
  "./SingletonLock", "./SingletonSocket", "./SingletonCookie", "./Crashpad", "./component_crx_cache",
];

function pipeline(first, second) {
  return new Promise((resolve, reject) => {
    const a = spawn(first[0], first.slice(1), { stdio: ["ignore", "pipe", "ignore"] });
    const b = spawn(second[0], second.slice(1), { stdio: ["pipe", "ignore", "ignore"] });
    let pending = 2;
    let failed = null;
    const done = (name) => (code) => {
      if (code !== 0 && !failed) failed = new Error(`${name} exited ${code}`);
      if (--pending === 0) (failed ? reject(failed) : resolve());
    };
    a.on("error", reject);
    b.on("error", reject);
    a.stdout.pipe(b.stdin);
    a.on("close", done(first[0]));
    b.on("close", done(second[0]));
  });
}

async function recipientOf(identityPath, ageKeygen) {
  const chunks = [];
  await new Promise((resolve, reject) => {
    const child = spawn(ageKeygen, ["-y", identityPath], { stdio: ["ignore", "pipe", "ignore"] });
    child.stdout.on("data", (c) => chunks.push(c));
    child.on("error", reject);
    child.on("close", (code) => (code === 0 ? resolve() : reject(new Error("age-keygen -y failed"))));
  });
  const recipient = Buffer.concat(chunks).toString("utf8").trim();
  if (!/^age1[0-9a-z]{20,}$/.test(recipient)) throw new Error("bad age identity");
  return recipient;
}

export async function restoreProfile({ encryptedPath, identityPath, profileDir, age = "age", tar = "tar" }) {
  await mkdir(profileDir, { recursive: true, mode: 0o700 });
  if (!existsSync(encryptedPath)) return { restored: false };
  await readFile(identityPath); // fail early (and clearly) when the key is missing
  await pipeline([age, "-d", "-i", identityPath, encryptedPath], [tar, "-x", "-C", profileDir, "-f", "-"]);
  return { restored: true };
}

export async function saveProfile({ encryptedPath, identityPath, profileDir, age = "age", ageKeygen = "age-keygen", tar = "tar" }) {
  const recipient = await recipientOf(identityPath, ageKeygen);
  const temporary = `${encryptedPath}.tmp-${process.pid}`;
  const excludes = EXCLUDES.flatMap((pattern) => [`--exclude=${pattern}`]);
  try {
    await pipeline(
      [tar, "-c", "-C", profileDir, ...excludes, "-f", "-", "."],
      [age, "-r", recipient, "-o", temporary],
    );
    await rename(temporary, encryptedPath);
  } finally {
    await rm(temporary, { force: true });
  }
  return { saved: true };
}

export async function wipeProfile(profileDir) {
  await rm(profileDir, { recursive: true, force: true });
}

