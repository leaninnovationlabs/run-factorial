/**
 * The Runtime page's vocabulary, kept as computations so the markup tests have less to assert
 * and the rules stay checkable one at a time.
 *
 * Each rule here mirrors one the server enforces (`orchestrator/factorial/runtimes.py`): the slug
 * shape, the type-to-family split, the type the login route still refuses. The page applies them
 * BEFORE the request so the error lands beside the field rather than in a red bar after it —
 * but the server's answer is still the one that counts.
 */

import type { RuntimeTemplateBuild, RuntimeType } from "../../api/types";

/** `orchestrator.factorial.runtimes.SLUG_RE`, and `ProcessConfig.runtime`'s rule: what a repo
 *  may write after `runtime:`. */
export const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

/** The same shape, as a length the proposal below has to stay under. */
const SLUG_MAX = 63;

export const RUNTIME_TYPES: readonly RuntimeType[] = [
  "codex_api",
  "codex_subscription",
  "codex_bedrock",
  "claude_api",
  "claude_subscription",
];

/** `runtime.adapters.e2b.smoke.VENDOR_KEYS`: the model key a type's session smoke bills. */
export function smokeKeyOf(
  type: RuntimeType,
): "OPENAI_API_KEY" | "ANTHROPIC_API_KEY" | "AWS_BEARER_TOKEN_BEDROCK" {
  if (type === "codex_bedrock") return "AWS_BEARER_TOKEN_BEDROCK";
  return type.startsWith("codex") ? "OPENAI_API_KEY" : "ANTHROPIC_API_KEY";
}

export const RUNTIME_TYPE_LABELS: Record<RuntimeType, string> = {
  codex_api: "Codex · API key",
  codex_subscription: "Codex · Subscription",
  codex_bedrock: "Codex · AWS Bedrock",
  claude_api: "Claude Code · API key",
  claude_subscription: "Claude Code · Subscription",
};

/** `orchestrator.factorial.runtimes.NOT_IMPLEMENTED_TYPES`: in the enum so a row can exist,
 *  refused by the worker and the login route until built. The form shows such a type, disabled.
 *  Empty since `claude_subscription` was built; kept so the next type can be listed before it
 *  is offered. */
const NOT_IMPLEMENTED: ReadonlySet<RuntimeType> = new Set();

export function typeAvailable(type: RuntimeType): boolean {
  return !NOT_IMPLEMENTED.has(type);
}

/** `orchestrator.factorial.runtimes.credential_name_for` (tech-stack decision 36): the one
 *  credential a runtime row holds for its type — what a login stores, what the adapter reads. */
const CREDENTIAL_NAME: Record<RuntimeType, string> = {
  codex_api: "OPENAI_API_KEY",
  codex_subscription: "CODEX_AUTH_JSON",
  codex_bedrock: "AWS_BEARER_TOKEN_BEDROCK",
  claude_api: "ANTHROPIC_API_KEY",
  claude_subscription: "CLAUDE_CODE_OAUTH_TOKEN",
};

const PLATFORM_MINTED = new Set<RuntimeType>(["codex_bedrock"]);

export function requiresStoredCredential(type: RuntimeType): boolean {
  return !PLATFORM_MINTED.has(type);
}

export function credentialNameFor(type: RuntimeType): string {
  return CREDENTIAL_NAME[type];
}

/** The codex bundle's name, so the paste-fallback and the device login store under one key. */
export const SUBSCRIPTION_CREDENTIAL = credentialNameFor("codex_subscription");

/** The copy for a credential that arrives as one pasted line: what to call it, what the empty
 *  field says, and where an operator gets one. The button is "Store <noun>" / "Replace <noun>". */
export interface CredentialForm {
  label: string;
  noun: "key" | "token";
  placeholder: string;
  hint: string;
}

/** Every type but the codex bundle, which comes in by device login or a pasted `auth.json`
 *  (`RuntimeDetail`'s `ConnectSubscription` and `PasteBundle`). A type listed here gets the
 *  one-field form the moment `typeAvailable` says so — nothing else has to change for it. */
const CREDENTIAL_FORMS: Partial<Record<RuntimeType, CredentialForm>> = {
  codex_api: {
    label: "OpenAI API key",
    noun: "key",
    placeholder: "Starts with sk-",
    hint: "Create one at platform.openai.com, under API keys.",
  },
  claude_api: {
    label: "Anthropic API key",
    noun: "key",
    placeholder: "Starts with sk-ant-",
    hint: "Create one at console.anthropic.com, under API keys.",
  },
  claude_subscription: {
    label: "Claude Code OAuth token",
    noun: "token",
    placeholder: "Starts with sk-ant-oat",
    hint: "Run 'claude setup-token' on your machine and paste the printed token.",
  },
};

export function credentialFormFor(type: RuntimeType): CredentialForm | null {
  return CREDENTIAL_FORMS[type] ?? null;
}

/** `orchestrator.factorial.runtimes.E2B_CREDENTIAL`: an E2B runtime's second credential, the key
 *  of the E2B team its sandboxes run in. It reaches the E2B client only, never a run. */
export const E2B_CREDENTIAL = "E2B_API_KEY";

export const E2B_KEY_FORM: CredentialForm = {
  label: "Sandbox API key",
  noun: "key",
  placeholder: "Starts with e2b_",
  hint: "Create one in the E2B dashboard, under API keys, in the team that this runtime's sandboxes run in.",
};

/** The template build an E2B runtime boots, or empty when it names none. */
export function templateOf(details: Record<string, unknown> | undefined): string {
  return typeof details?.template === "string" ? details.template : "";
}

/** The runtime's own harness first, as the builder names it (`claude-<version>`), then
 *  the builds that passed the session smoke; newest first within each, as listed. */
export function orderBuilds(builds: readonly RuntimeTemplateBuild[]): RuntimeTemplateBuild[] {
  return [...builds].sort(
    (a, b) =>
      Number(b.matches_harness) - Number(a.matches_harness) ||
      Number(b.verified) - Number(a.verified),
  );
}

/** The build a picker offers first, from a list in `orderBuilds` order: one of the runtime's own
 *  CLI, never `current`. A build of the other CLI fails at its first run, so none is offered
 *  when no build matches. */
export function defaultBuild(
  ordered: readonly RuntimeTemplateBuild[],
  current = "",
): RuntimeTemplateBuild | null {
  return ordered.find((b) => b.matches_harness && b.pin !== current) ?? null;
}

/** What the team's build list says of the build `pin` names: it passed the session smoke, it
 *  did not, or the list does not hold it. Null while the list is unknown. */
export function buildVerdict(
  builds: readonly RuntimeTemplateBuild[] | undefined,
  pin: string,
): "passed" | "unverified" | "unlisted" | null {
  if (builds === undefined) return null;
  const build = builds.find((each) => each.pin === pin);
  if (build === undefined) return "unlisted";
  return build.verified ? "passed" : "unverified";
}

export type CredentialState = "connected" | "none";

/** Whether a runtime holds the one credential its type is connected by. Exact-name for every
 *  type: a stray name under another key would not get it past login. */
export function credentialState(type: RuntimeType, names: readonly string[]): CredentialState {
  if (!requiresStoredCredential(type)) return "connected";
  return names.includes(credentialNameFor(type)) ? "connected" : "none";
}

/**
 * The slug the form proposes from a name: lowercase ASCII, one dash per run of anything else,
 * accents folded onto their base letter, and cut to the server's length without ending on a
 * dash. Empty when nothing survives — the field then simply waits for the operator.
 */
export function suggestSlug(name: string): string {
  const folded = name
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return folded.slice(0, SLUG_MAX).replace(/-+$/, "");
}

/**
 * Where a runtime's agents run (tech-stack decision 25): `details.isolation` on the row. The
 * API refuses any other value, and a row that predates the field runs on the host.
 */
export type IsolationStrategy = "local_cli" | "e2b";

/** A runtime's sandbox, as the list and its own page name it. */
export function sandboxLabelOf(runtime: {
  details?: Record<string, unknown>;
  sandbox_key: "own" | "managed";
}): string {
  if (isolationOf(runtime.details) === "local_cli") return "On the host";
  return runtime.sandbox_key === "managed" ? "Factorial-managed sandbox" : "E2B sandbox";
}

export function isolationOf(details: Record<string, unknown> | undefined): IsolationStrategy {
  return details?.isolation === "e2b" ? "e2b" : "local_cli";
}

export type BedrockPartition = "gov" | "commercial";

export const GOV_BEDROCK_REGION = "us-gov-west-1";
export const GOV_BEDROCK_MODEL = "us-gov.xai.grok-4.6";
export const COMMERCIAL_BEDROCK_REGION = "us-east-1";
export const COMMERCIAL_BEDROCK_MODEL = "us.xai.grok-4.6";

export function bedrockRegionOf(details: Record<string, unknown> | undefined): string {
  return typeof details?.region === "string" && details.region.trim()
    ? details.region.trim()
    : GOV_BEDROCK_REGION;
}

export function bedrockModelOf(details: Record<string, unknown> | undefined): string {
  if (typeof details?.model === "string" && details.model.trim()) return details.model.trim();
  return bedrockRegionOf(details).includes("-gov-") ? GOV_BEDROCK_MODEL : COMMERCIAL_BEDROCK_MODEL;
}

export function bedrockPartitionOf(details: Record<string, unknown> | undefined): BedrockPartition {
  return bedrockRegionOf(details).includes("-gov-") ? "gov" : "commercial";
}

export function bedrockDefaults(partition: BedrockPartition): { region: string; model: string } {
  return partition === "gov"
    ? { region: GOV_BEDROCK_REGION, model: GOV_BEDROCK_MODEL }
    : { region: COMMERCIAL_BEDROCK_REGION, model: COMMERCIAL_BEDROCK_MODEL };
}

// --- metadata ----------------------------------------------------------------------------------

/** `orchestrator.factorial.runtimes.METADATA_KEY_RE` and its two limits: what one row of the
 *  metadata table may hold, and how many rows there may be. */
export const METADATA_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
export const METADATA_MAX_KEYS = 32;
export const METADATA_MAX_VALUE_LEN = 1024;

/** One row of the table. `id` is the row's identity across edits — the key is what the operator
 *  is typing, so it cannot be. */
export interface MetadataRow {
  id: number;
  key: string;
  value: string;
}

export type MetadataProblem =
  | { kind: "ok" }
  | { kind: "row"; id: number; field: "key" | "value"; message: string }
  | { kind: "map"; message: string };

/**
 * The first thing the server would refuse, so the note lands beside the field rather than in a
 * red bar after the request. `ok` means the rows are a map the API accepts.
 */
export function metadataProblem(rows: readonly MetadataRow[]): MetadataProblem {
  if (rows.length > METADATA_MAX_KEYS) {
    return { kind: "map", message: `At most ${METADATA_MAX_KEYS} rows.` };
  }
  const seen = new Set<string>();
  for (const row of rows) {
    if (row.key === "") {
      return { kind: "row", id: row.id, field: "key", message: "A key is required." };
    }
    if (!METADATA_KEY_RE.test(row.key)) {
      return {
        kind: "row",
        id: row.id,
        field: "key",
        message:
          "Use 1 to 64 letters, digits, underscores, dots or hyphens. Start with a letter or digit.",
      };
    }
    if (seen.has(row.key)) {
      return { kind: "row", id: row.id, field: "key", message: "This key is used twice." };
    }
    seen.add(row.key);
    if (row.value.length > METADATA_MAX_VALUE_LEN) {
      return {
        kind: "row",
        id: row.id,
        field: "value",
        message: `At most ${METADATA_MAX_VALUE_LEN} characters.`,
      };
    }
  }
  return { kind: "ok" };
}

export function metadataToRows(metadata: Record<string, string>): MetadataRow[] {
  return Object.entries(metadata).map(([key, value], id) => ({ id, key, value }));
}

export function rowsToMetadata(rows: readonly MetadataRow[]): Record<string, string> {
  return Object.fromEntries(rows.map((row) => [row.key, row.value]));
}
