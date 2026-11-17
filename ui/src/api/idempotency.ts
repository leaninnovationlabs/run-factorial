/** A fresh idempotency key for a request the API answers once per key. */
export function newIdempotencyKey(): string {
  // `getRandomValues`, not `randomUUID`: the dashboard may be served over plain http off
  // localhost, where `randomUUID` does not exist.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
