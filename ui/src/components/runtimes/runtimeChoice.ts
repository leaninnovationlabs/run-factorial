/**
 * The create page's card rules, as computations: which stored type and isolation three card
 * choices make, which cards a deployment's offered list shows, and which keys a choice
 * asks for. The offered list (`GET /api/runtime-options`) is the authority; the API refuses what
 * it does not list, so these rules only keep the page from asking.
 */

import type { RuntimeType } from "../../api/types";
import type { Role } from "../../status";
import type { RadioOption } from "../ui";
import {
  type BedrockPartition,
  bedrockDefaults,
  bedrockPartitionOf,
  credentialNameFor,
  E2B_CREDENTIAL,
  type IsolationStrategy,
  requiresStoredCredential,
} from "./runtimes";

export type Cli = "claude" | "codex";
export type Auth = "api_key" | "subscription" | "bedrock_commercial" | "bedrock_govcloud";
export type Sandbox = "host" | "cloud" | "self_hosted";
export type Choice = { cli: Cli; auth: Auth; sandbox: Sandbox };

export interface CardInfo<T extends string> {
  value: T;
  label: string;
  description: string;
}

export const CLI_CARDS: readonly CardInfo<Cli>[] = [
  { value: "claude", label: "Claude Code", description: "Anthropic's coding agent." },
  { value: "codex", label: "Codex", description: "OpenAI's coding agent." },
];

const CLI_ORDER = CLI_CARDS.map((card) => card.value);

const AUTH_ORDER: readonly Auth[] = [
  "api_key",
  "subscription",
  "bedrock_commercial",
  "bedrock_govcloud",
];

const PROVIDER: Record<Cli, { models: string; apiKey: string; plan: string }> = {
  claude: { models: "Claude", apiKey: "An Anthropic API key", plan: "Claude" },
  codex: { models: "OpenAI", apiKey: "An OpenAI API key", plan: "ChatGPT" },
};

export function authCards(cli: Cli): CardInfo<Auth>[] {
  const { models, apiKey, plan } = PROVIDER[cli];
  return [
    { value: "api_key", label: "API key", description: `${apiKey}, billed per token.` },
    { value: "subscription", label: "Subscription", description: `Your ${plan} plan.` },
    {
      value: "bedrock_commercial",
      label: "AWS Bedrock (Commercial)",
      description: `${models} models through your own AWS account, in commercial regions.`,
    },
    {
      value: "bedrock_govcloud",
      label: "AWS Bedrock (GovCloud)",
      description: `${models} models through your own AWS GovCloud (US) account.`,
    },
  ];
}

export const SANDBOX_CARDS: readonly CardInfo<Sandbox>[] = [
  {
    value: "host",
    label: "On the host",
    description: "The agent runs on the machine that runs Factorial, and can read its files.",
  },
  {
    value: "cloud",
    label: "Managed cloud sandbox",
    description: "A fresh sandbox per run. Keys stay outside it.",
  },
  {
    value: "self_hosted",
    label: "Self-hosted sandbox",
    description: "A sandbox in your own infrastructure.",
  },
];

const DEFAULT_SANDBOX: Sandbox = "host";
/** The default sandbox first, so a default or a reconciled choice lands on it. */
const SANDBOX_ORDER: readonly Sandbox[] = [
  DEFAULT_SANDBOX,
  ...SANDBOX_CARDS.map((card) => card.value).filter((each) => each !== DEFAULT_SANDBOX),
];

/** Each type's cards. `codex_bedrock` answers to both Bedrock cards: its stored region, not its
 *  type, names the partition. */
const AXES: Record<RuntimeType, { cli: Cli; auths: readonly [Auth, ...Auth[]] }> = {
  claude_api: { cli: "claude", auths: ["api_key"] },
  claude_subscription: { cli: "claude", auths: ["subscription"] },
  codex_api: { cli: "codex", auths: ["api_key"] },
  codex_subscription: { cli: "codex", auths: ["subscription"] },
  codex_bedrock: { cli: "codex", auths: ["bedrock_govcloud", "bedrock_commercial"] },
};

/** Null for Claude on Bedrock: no stored type exists for it yet. */
export function typeFor(cli: Cli, auth: Auth): RuntimeType | null {
  const types = Object.keys(AXES) as RuntimeType[];
  return types.find((type) => AXES[type].cli === cli && AXES[type].auths.includes(auth)) ?? null;
}

const PARTITION_AUTH: Record<BedrockPartition, Auth> = {
  gov: "bedrock_govcloud",
  commercial: "bedrock_commercial",
};

export function axesOf(
  type: RuntimeType,
  details?: Record<string, unknown>,
): { cli: Cli; auth: Auth } {
  const { cli, auths } = AXES[type];
  const auth = type === "codex_bedrock" ? PARTITION_AUTH[bedrockPartitionOf(details)] : auths[0];
  return { cli, auth };
}

export const ISOLATION_FOR: Record<Sandbox, IsolationStrategy | null> = {
  host: "local_cli",
  cloud: "e2b",
  self_hosted: null,
};

/** `<type>@<isolation>`, the string `GET /api/runtime-options` lists. */
export function offerKey(type: RuntimeType, isolation: IsolationStrategy): string {
  return `${type}@${isolation}`;
}

export type CardLozenge = "Not recommended for prod";
export interface CardState {
  enabled: boolean;
  lozenge: CardLozenge | null;
  reason: string | null;
}

const UNREADABLE = "Couldn't load the available options";
const NOT_OFFERED = "Not available on this installation";
const UNBUILT: CardState = { enabled: false, lozenge: null, reason: null };

/** Whether the list holds this pair on `sandbox`, or on any sandbox when `sandbox` is null. */
function offers(offered: ReadonlySet<string>, cli: Cli, auth: Auth, sandbox: Sandbox | null) {
  const type = typeFor(cli, auth);
  if (type === null) return false;
  return (sandbox === null ? SANDBOX_ORDER : [sandbox]).some((each) => {
    const isolation = ISOLATION_FOR[each];
    return isolation !== null && offered.has(offerKey(type, isolation));
  });
}

/** `ok` is null when the offered list could not be read. */
function state(
  ok: boolean | null,
  { reason = NOT_OFFERED, lozenge = null }: { reason?: string; lozenge?: CardLozenge | null } = {},
): CardState {
  if (ok === null) return { enabled: false, lozenge, reason: UNREADABLE };
  return { enabled: ok, lozenge, reason: ok ? null : reason };
}

function statesOf<T extends string>(
  order: readonly T[],
  stateOf: (value: T) => CardState,
): Record<T, CardState> {
  return Object.fromEntries(order.map((each) => [each, stateOf(each)])) as Record<T, CardState>;
}

function cliStates(offered: ReadonlySet<string> | null): Record<Cli, CardState> {
  return statesOf(CLI_ORDER, (cli) =>
    state(offered && AUTH_ORDER.some((auth) => offers(offered, cli, auth, null)), {
      reason: `${labelOf(CLI_CARDS, cli)} isn't available on this installation`,
    }),
  );
}

function authStates(offered: ReadonlySet<string> | null, cli: Cli): Record<Auth, CardState> {
  return statesOf(AUTH_ORDER, (auth) =>
    typeFor(cli, auth) === null ? UNBUILT : state(offered && offers(offered, cli, auth, null)),
  );
}

function sandboxStates(
  offered: ReadonlySet<string> | null,
  cli: Cli,
  auth: Auth,
): Record<Sandbox, CardState> {
  return statesOf(SANDBOX_ORDER, (sandbox) =>
    sandbox === "self_hosted"
      ? UNBUILT
      : state(
          offered && offers(offered, cli, auth, sandbox),
          sandbox === "host" ? { lozenge: "Not recommended for prod" } : {},
        ),
  );
}

/** `offered === null` means the list could not be read: nothing is enabled. */
export interface CardStates {
  cli: Record<Cli, CardState>;
  auth: Record<Auth, CardState>;
  sandbox: Record<Sandbox, CardState>;
}

export function cardStates(offered: ReadonlySet<string> | null, choice: Choice): CardStates {
  return {
    cli: cliStates(offered),
    auth: authStates(offered, choice.cli),
    sandbox: sandboxStates(offered, choice.cli, choice.auth),
  };
}

export function defaultChoice(offered: ReadonlySet<string>): Choice | null {
  for (const cli of CLI_ORDER) {
    for (const auth of AUTH_ORDER) {
      for (const sandbox of SANDBOX_ORDER) {
        if (offers(offered, cli, auth, sandbox)) return { cli, auth, sandbox };
      }
    }
  }
  return null;
}

function firstEnabled<T extends string>(
  order: readonly T[],
  states: Record<T, CardState>,
  now: T,
): T {
  return states[now].enabled ? now : (order.find((each) => states[each].enabled) ?? now);
}

/** Moves a choice the offered list no longer allows to the first enabled card of its group,
 *  CLI first: the CLI decides which authentications exist, and both decide the sandboxes. */
export function reconcile(offered: ReadonlySet<string> | null, choice: Choice): Choice {
  if (offered === null) return choice;
  const cli = firstEnabled(CLI_ORDER, cliStates(offered), choice.cli);
  const auth = firstEnabled(AUTH_ORDER, authStates(offered, cli), choice.auth);
  const sandbox = firstEnabled(SANDBOX_ORDER, sandboxStates(offered, cli, auth), choice.sandbox);
  return { cli, auth, sandbox };
}

export type CodexSignIn = "chatgpt" | "paste";

/** A ChatGPT sign-in happens on the runtime's page after create, so the page stores and checks
 *  no login for it. */
export function deferredLogin(type: RuntimeType | null, codexSignIn: CodexSignIn): boolean {
  return type === "codex_subscription" && codexSignIn === "chatgpt";
}

/** What a create or a key check sends as `details`: the sandbox, and for Bedrock the region and
 *  model of the chosen partition. */
export function detailsFor(choice: Choice): Record<string, unknown> {
  const isolation = ISOLATION_FOR[choice.sandbox];
  const details: Record<string, unknown> = isolation === null ? {} : { isolation };
  const partition = (Object.keys(PARTITION_AUTH) as BedrockPartition[]).find(
    (each) => PARTITION_AUTH[each] === choice.auth,
  );
  if (typeFor(choice.cli, choice.auth) === "codex_bedrock" && partition) {
    Object.assign(details, bedrockDefaults(partition));
  }
  return details;
}

/** The credential names the page asks for. */
export function fieldsFor(choice: Choice, codexSignIn: CodexSignIn): string[] {
  const type = typeFor(choice.cli, choice.auth);
  const names: string[] = [];
  if (type !== null && requiresStoredCredential(type) && !deferredLogin(type, codexSignIn)) {
    names.push(credentialNameFor(type));
  }
  if (choice.sandbox === "cloud") names.push(E2B_CREDENTIAL);
  return names;
}

const LOZENGE_ROLE: Record<CardLozenge, Role> = {
  "Not recommended for prod": "warning",
};

/** The cards that can be chosen: a card the deployment leaves out, or one not built yet, is not
 *  shown at all (#385). */
export function cardOptions<T extends string>(
  cards: readonly CardInfo<T>[],
  states: Record<T, CardState>,
): RadioOption[] {
  return cards
    .filter((card) => states[card.value].enabled)
    .map((card) => {
      const { enabled, lozenge } = states[card.value];
      return {
        value: card.value,
        label: card.label,
        description: card.description,
        isDisabled: !enabled,
        lozenge: lozenge ? { text: lozenge, appearance: LOZENGE_ROLE[lozenge] } : undefined,
      };
    });
}

function labelOf<T extends string>(cards: readonly CardInfo<T>[], value: T): string {
  return cards.find((card) => card.value === value)?.label ?? value;
}

/** The CLI and authentication card names, which a stored type reads as too. */
export function axisLabels({ cli, auth }: { cli: Cli; auth: Auth }): { cli: string; auth: string } {
  return { cli: labelOf(CLI_CARDS, cli), auth: labelOf(authCards(cli), auth) };
}

/** The card names a choice reads as, for the summary and the suggested name. */
export function choiceLabels(choice: Choice): { cli: string; auth: string; sandbox: string } {
  return { ...axisLabels(choice), sandbox: labelOf(SANDBOX_CARDS, choice.sandbox) };
}

export function suggestName(choice: Choice): string {
  const labels = choiceLabels(choice);
  const base = `${labels.cli} · ${labels.auth}`;
  return choice.sandbox === "cloud" ? `${base} · ${labels.sandbox}` : base;
}

const AUTH_WORD: Record<Auth, string> = {
  api_key: "api",
  subscription: "sub",
  bedrock_commercial: "bedrock",
  bedrock_govcloud: "bedrock",
};

/** The slug a choice proposes, the shape processes name runtimes by: `claude-api`,
 *  `codex-sub`, `claude-api-on-e2b`. */
export function suggestSlugFor(choice: Choice): string {
  const base = `${choice.cli}-${AUTH_WORD[choice.auth]}`;
  return choice.sandbox === "cloud" ? `${base}-on-e2b` : base;
}

/** The cards a slug of that shape names, or null for any other slug. Bedrock names no partition,
 *  so it sets no auth card. */
export function choiceFromSlug(slug: string): Partial<Choice> | null {
  const match = /^(claude|codex)-(api|sub|bedrock)(-on-e2b)?$/.exec(slug);
  if (!match) return null;
  const [, cli, word, cloud] = match;
  const auth = { api: "api_key", sub: "subscription" }[word as "api" | "sub"];
  return {
    cli: cli as Cli,
    ...(auth ? { auth: auth as Auth } : {}),
    sandbox: cloud ? "cloud" : "host",
  };
}

/** Why the current choice cannot be created, or null. `reconcile` keeps a choice whose group has
 *  no enabled card, so the chosen card can be a disabled one. */
export function choiceProblem(
  offered: ReadonlySet<string> | null,
  choice: Choice,
  states: CardStates,
): string | null {
  if (offered === null) return `${UNREADABLE}.`;
  const labels = choiceLabels(choice);
  const chosen: [string, CardState][] = [
    [labels.cli, states.cli[choice.cli]],
    [labels.auth, states.auth[choice.auth]],
    [labels.sandbox, states.sandbox[choice.sandbox]],
  ];
  for (const [label, card] of chosen) {
    if (!card.enabled) return `${label}: ${card.reason ?? card.lozenge ?? NOT_OFFERED}.`;
  }
  return null;
}
