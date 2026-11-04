import { useId, useState } from "react";
import { useCan } from "../../api/organizationQueries";
import { useDefaultBranchConfig, useProcessTemplates, useRuntimes } from "../../api/queries";
import type { AgentDetail, ProjectDetail, Runtime } from "../../api/types";
import { plural } from "../../format";
import { listSetting, RUNTIME_ALLOWLIST } from "../configuration/PermitsBody";
import { isProcessConfigLike } from "../flow";
import {
  Button,
  ErrorMessage,
  Icon,
  LinkButton,
  Pressable,
  Select,
  Spinner,
  Tab,
  TabList,
  TabPanel,
  Tabs,
} from "../ui";
import type { AgentsSource } from "./AgentCards";
import { laneCounts } from "./agentSteps";
import { LaneView } from "./LaneView";
import { laneNameFor } from "./laneName";
import { permitsToAdd } from "./runtimePermit";

export const GALLERY_SLOTS = 4;

export type GalleryMode = "branch-missing" | "new-lane";

/** What an import brings in: one template's lanes, each under the name it takes by default and
 *  on the runtime picked for them, with the permits that runtime still needs; or the default
 *  branch's folder as it is. */
export type GalleryChoice =
  | {
      kind: "template";
      title: string;
      lanes: { name: string; yaml: string }[];
      runtime: string;
      permit: string[];
    }
  | { kind: "default-branch" };

type RuntimePick = { slug: string; permit: string[] };

type CardLane = { key: string; content: Record<string, unknown>; agents: AgentDetail[] };

/** One starting point on the shelf, in the shape the preview draws: a title, and its lanes. */
type Card = {
  id: string;
  title: string;
  description: string;
  lanes: CardLane[];
  /** Why the default branch's folder does not parse; the copy is still allowed. */
  error?: string | null;
  /** Null while the choice still lacks the runtime it needs. */
  choice: (taken: readonly string[], runtime: RuntimePick | null) => GalleryChoice | null;
  importLabel: string;
  /** A template leaves its `runtime:` line commented out, and the server refuses a lane that
   *  names none; the default branch's files carry their own. */
  needsRuntime: boolean;
};

const ready = (list: AgentsSource["list"]): AgentsSource => ({ list, pending: false, error: null });

function counts(content: Record<string, unknown>): string {
  if (!isProcessConfigLike(content)) return "";
  const { steps, agents, gates } = laneCounts(content);
  return [plural(steps, "step"), plural(agents, "agent"), plural(gates, "gate")].join(" · ");
}

/** A card's one-line size: a lone lane's steps, agents and gates, or how many lanes it holds. */
function size(card: Card): string {
  if (card.error) return card.error;
  const [only] = card.lanes;
  return card.lanes.length === 1 && only
    ? counts(only.content)
    : plural(card.lanes.length, "process", "processes");
}

/** The runtime the select starts on: the one runtime the project permits, else the only one
 *  offered, else none. */
function presetRuntime(slugs: readonly string[], permitted: readonly string[]): string | null {
  const allowed = slugs.filter((slug) => permitted.includes(slug));
  const [only] = allowed.length === 1 ? allowed : slugs.length === 1 ? slugs : [];
  return only ?? null;
}

function runtimeLabel(runtime: Runtime): string {
  return runtime.name === runtime.slug ? runtime.slug : `${runtime.name} · ${runtime.slug}`;
}

/** Each lane's name, in order: its own key, or the next free one — counting the lanes the
 *  project has and the names earlier lanes of the same import took. */
export function defaultLaneNames(keys: readonly string[], taken: readonly string[]): string[] {
  const names: string[] = [...taken];
  return keys.map((key) => {
    const name = laneNameFor(key, names);
    names.push(name);
    return name;
  });
}

/**
 * Where processes come from: one template — a folder of lanes, imported together — or the
 * default branch's folder as it is. Four slots always, so the row reads as a shelf with room on
 * it; a vacant slot is decoration, never a control. Nothing is written here — `onImport` hands
 * the choice to the section, which commits it.
 */
export function ConfigGallery({
  project,
  mode,
  busy,
  error,
  onImport,
  onCancel,
}: {
  project: ProjectDetail;
  mode: GalleryMode;
  busy: boolean;
  error: unknown;
  onImport: (choice: GalleryChoice) => void;
  onCancel?: () => void;
}) {
  const templates = useProcessTemplates();
  const fromBranch = useDefaultBranchConfig(project.id, mode === "branch-missing");
  const taken = project.processes.map((lane) => lane.process_key);
  // Its slot is held while it loads: a template picked first seeds an empty branch, and the copy
  // is never offered again.
  const reading = mode === "branch-missing" && fromBranch.isLoading;

  const branchConfig = mode === "branch-missing" ? fromBranch.data : undefined;
  const cards: Card[] = [
    ...(branchConfig
      ? [
          {
            id: "default-branch",
            title: `From ${branchConfig.branch}`,
            description: `Copies the ${branchConfig.config_dir}/ folder from ${branchConfig.branch}, unchanged.`,
            lanes: branchConfig.lanes,
            error: branchConfig.error,
            choice: () => ({ kind: "default-branch" as const }),
            importLabel: `Import ${branchConfig.branch}'s config`,
            needsRuntime: false,
          },
        ]
      : []),
    ...(templates.data?.templates ?? []).map((template) => ({
      id: `template:${template.key}`,
      title: template.title,
      description: template.description,
      lanes: template.lanes,
      choice: (names: readonly string[], runtime: RuntimePick | null): GalleryChoice | null => {
        if (runtime === null) return null;
        const laneNames = defaultLaneNames(
          template.lanes.map((lane) => lane.key),
          names,
        );
        return {
          kind: "template",
          title: template.title,
          lanes: template.lanes.map((lane, index) => ({
            name: laneNames[index] ?? lane.key,
            yaml: lane.yaml,
          })),
          runtime: runtime.slug,
          permit: runtime.permit,
        };
      },
      importLabel: "Import process template",
      needsRuntime: true,
    })),
  ];
  const [pickedId, setPickedId] = useState<string | null>(null);
  const [laneKey, setLaneKey] = useState<string | null>(null);
  const [selectedNode, selectNode] = useState<string | null>(null);
  const picked = cards.find((card) => card.id === pickedId) ?? null;

  // The runtimes route needs `runtimes:manage`. A reader without it is offered the runtimes the
  // project already permits, which are the only ones such a reader could name in any case.
  const canRuntimes = useCan("runtimes:manage");
  const runtimes = useRuntimes({ enabled: canRuntimes });
  const permitted = listSetting(project.settings, RUNTIME_ALLOWLIST);
  const registered = canRuntimes ? (runtimes.data ?? []) : [];
  const offered = canRuntimes
    ? registered.map((rt) => ({ value: rt.slug, label: runtimeLabel(rt) }))
    : permitted.map((slug) => ({ value: slug, label: slug }));
  const slugs = offered.map((option) => option.value);
  const runtimesFailed = canRuntimes && !runtimes.data ? runtimes.error : null;
  const failedId = useId();
  const [chosenRuntime, chooseRuntime] = useState<string | null>(null);
  // A pick whose runtime has since gone is no pick at all.
  const runtime =
    chosenRuntime === null
      ? presetRuntime(slugs, permitted)
      : slugs.includes(chosenRuntime)
        ? chosenRuntime
        : null;
  const askRuntime = picked?.needsRuntime === true;
  const runtimeReason =
    !askRuntime || runtimesFailed
      ? undefined
      : canRuntimes && runtimes.isPending
        ? "Loading the runtimes…"
        : offered.length === 0
          ? canRuntimes
            ? "No runtime yet. Create one first."
            : "No runtime yet. Ask an organization admin to create one."
          : runtime === null
            ? "Pick a runtime first"
            : undefined;
  const toPermit = askRuntime && runtime ? permitsToAdd(project, [runtime], registered) : [];

  function pick(id: string) {
    setPickedId(id);
    setLaneKey(null);
    selectNode(null);
  }

  return (
    <div>
      {/* The import is the page's one action, so it sits with the heading rather than under a
          preview that can run a screen long. */}
      <div className="flex flex-wrap items-start justify-between gap-4 p-4 pb-0">
        <div className="min-w-0 flex-1 space-y-1">
          <h3 className="text-heading-sm text-default">Import process config</h3>
          <p className="text-body text-subtle">
            {mode === "branch-missing"
              ? "This repository has no factorial branch yet. Pick a template to start from. Nothing is written until you import it."
              : "Pick a template to add its processes. Nothing is written until you import it."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {onCancel && (
            <Button appearance="subtle" onClick={onCancel}>
              Cancel
            </Button>
          )}
          {askRuntime && offered.length > 0 && (
            <Select
              aria-label="Runtime"
              width="medium"
              value={runtime ?? ""}
              disabled={busy}
              onChange={(event) => chooseRuntime(event.target.value || null)}
              options={[{ value: "", label: "Pick a runtime", isDisabled: true }, ...offered]}
            />
          )}
          {askRuntime && canRuntimes && runtimes.data?.length === 0 && (
            <LinkButton to="/runtimes/new">Create a runtime</LinkButton>
          )}
          <Button
            appearance="primary"
            isLoading={busy}
            isDisabled={picked === null}
            disabledReason={runtimeReason}
            aria-disabled={askRuntime && runtimesFailed ? true : undefined}
            aria-describedby={askRuntime && runtimesFailed ? failedId : undefined}
            onClick={() => {
              const choice = picked?.choice(
                taken,
                runtime === null ? null : { slug: runtime, permit: toPermit },
              );
              if (choice) onImport(choice);
            }}
          >
            {toPermit.length > 0
              ? `Permit ${toPermit.join(", ")} and import`
              : (picked?.importLabel ?? "Import process template")}
          </Button>
        </div>
      </div>
      {toPermit.length > 0 && (
        <p className="px-4 pt-2 text-body-sm text-subtle">
          The import also permits {toPermit.join(", ")} for this project.
        </p>
      )}
      {askRuntime && runtimesFailed && (
        <div id={failedId}>
          <ErrorMessage
            error={runtimesFailed}
            title="Couldn't load the runtimes"
            onRetry={() => runtimes.refetch()}
            className="mx-4 mt-3"
          />
        </div>
      )}
      <ErrorMessage error={error} className="mx-4 mt-3" />

      {templates.isPending ? (
        <div className="p-4">
          <Spinner label="Loading the templates…" />
        </div>
      ) : templates.error ? (
        <ErrorMessage error={templates.error} className="m-4" />
      ) : (
        <ul className="grid grid-cols-2 gap-2 p-4 lg:grid-cols-4">
          {cards.map((card) => (
            <li key={card.id}>
              <Pressable
                data-gallery-card
                isSelected={card.id === pickedId}
                onClick={() => pick(card.id)}
                className="flex h-full w-full flex-col items-start justify-start p-3"
              >
                <span className="flex w-full items-start justify-between gap-2">
                  <span className="block text-heading-xs">{card.title}</span>
                  {card.id === pickedId && (
                    <Icon glyph="status-success" label="Picked" className="text-icon-selected" />
                  )}
                </span>
                <span className="block text-body-sm text-subtle">{card.description}</span>
                <span className="mt-2 block text-body-sm text-subtlest">{size(card)}</span>
              </Pressable>
            </li>
          ))}
          {reading && (
            <li aria-busy="true">
              <div className="flex h-full min-h-24 items-center justify-center rounded-md border border-default p-3 text-body-sm text-subtle">
                Loading the default branch's config…
              </div>
            </li>
          )}
          {Array.from(
            { length: Math.max(0, GALLERY_SLOTS - cards.length - (reading ? 1 : 0)) },
            (_, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: vacant slots are interchangeable
              <li key={`vacant-${index}`} aria-hidden="true">
                <div
                  data-gallery-vacant
                  aria-hidden="true"
                  className="flex h-full min-h-24 items-center justify-center rounded-md border border-dashed border-default bg-surface-sunken text-body-sm text-disabled"
                >
                  Empty slot
                </div>
              </li>
            ),
          )}
        </ul>
      )}

      {mode === "branch-missing" && fromBranch.error && (
        <ErrorMessage error={fromBranch.error} className="mx-4 mb-4" />
      )}

      {picked === null ? (
        <p className="px-4 pb-4 text-body-sm text-subtle">
          Pick a template to see its processes, and the agents, skills and MCP servers each one
          uses.
        </p>
      ) : (
        <Preview
          card={picked}
          laneKey={laneKey}
          onLaneKey={(key) => {
            setLaneKey(key);
            selectNode(null);
          }}
          selectedNode={selectedNode}
          onSelectNode={selectNode}
        />
      )}
    </div>
  );
}

/**
 * The picked template, under a band that names it, its lanes as tabs: each tab's panel is that
 * lane's graph and the agents, skills and MCP servers it uses — so everything under the band
 * reads as that template's, and each tool as that lane's.
 */
function Preview({
  card,
  laneKey,
  onLaneKey,
  selectedNode,
  onSelectNode,
}: {
  card: Card;
  laneKey: string | null;
  onLaneKey: (key: string) => void;
  selectedNode: string | null;
  onSelectNode: (id: string | null) => void;
}) {
  const lane = card.lanes.find((entry) => entry.key === laneKey) ?? card.lanes[0];
  return (
    <section data-gallery-preview aria-label={`What ${card.title} brings`}>
      <div className="border-t border-default bg-surface-sunken px-4 pt-3">
        <h4 className="text-heading-xs text-default">{card.title}</h4>
        <p className="pb-2 text-body-sm text-subtle">
          {card.lanes.length === 1
            ? "One process, and the agents, skills and MCP servers it uses:"
            : `${card.lanes.length} processes, all imported together. Each tab is one process, and the agents, skills and MCP servers it uses:`}
        </p>
      </div>
      {lane && (
        <Tabs id={`gallery-${card.id}`} selected={lane.key} onChange={onLaneKey}>
          <div className="bg-surface-sunken px-4">
            <TabList label="Processes">
              {card.lanes.map((entry) => (
                <Tab key={entry.key} value={entry.key}>
                  <Icon glyph="lane" />
                  {entry.key}
                </Tab>
              ))}
            </TabList>
          </div>
          {card.lanes.map((entry) => (
            <TabPanel key={entry.key} value={entry.key}>
              {isProcessConfigLike(entry.content) && (
                <LaneView
                  config={entry.content}
                  agents={ready(entry.agents)}
                  selectedId={selectedNode}
                  onSelect={onSelectNode}
                  caption={`process · ${entry.key}`}
                />
              )}
            </TabPanel>
          ))}
        </Tabs>
      )}
    </section>
  );
}
