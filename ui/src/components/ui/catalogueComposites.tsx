/** The dev catalogue's parts that compose design primitives: tabs, states, overlays, a table. */

import { useState } from "react";
import { BarList } from "./BarList";
import { Button } from "./Button";
import { Code, CodeBlock, CopyButton } from "./Code";
import { ConfirmPopup } from "./ConfirmPopup";
import { Row, Section } from "./catalogue";
import { DataTable } from "./DataTable";
import { ConfirmDialog, Drawer, ModalDialog, TextDrawer } from "./dialog";
import { showFlag } from "./Flag";
import { InlineEditText } from "./InlineEdit";
import { Legend } from "./Legend";
import { StatusLozenge } from "./Lozenge";
import { Metric } from "./Metric";
import { InlineMessage } from "./messages";
import { Panel } from "./Panel";
import { DropdownMenu } from "./Popup";
import { RelativeTime } from "./RelativeTime";
import { StackedBar } from "./StackedBar";
import { StatTile } from "./StatTile";
import { EmptyState, Skeleton, SkeletonRows, Spinner } from "./states";
import { SegmentedControl, Tab, TabList, TabPanel, Tabs } from "./Tabs";
import { Tooltip } from "./Tooltip";

const YAML = `specVersion: factorial/v1
kind: Process
steps:
  - name: spec
    agent: spec-writer
    next: spec_gate`;

type TicketRow = { id: number; key: string; status: string; cost: string; updated: string };
const ROWS: TicketRow[] = [
  {
    id: 1,
    key: "acme/widgets#42",
    status: "running",
    cost: "$ 4.20",
    updated: "2026-09-14T09:12:00Z",
  },
  {
    id: 2,
    key: "acme/widgets#57",
    status: "waiting_gate",
    cost: "$ 1.05",
    updated: "2026-09-13T16:40:00Z",
  },
  {
    id: 3,
    key: "acme/gadgets#7",
    status: "blocked",
    cost: "$ 12.90",
    updated: "2026-09-12T08:02:00Z",
  },
];

export function Navigation() {
  const [tab, setTab] = useState("tickets");
  const [order, setOrder] = useState("newest");
  return (
    <Section title="Tabs and segments">
      <Tabs id="catalogue-tabs" selected={tab} onChange={setTab}>
        <TabList label="Project">
          <Tab value="tickets" badge={12}>
            Tickets
          </Tab>
          <Tab value="configuration">Configuration</Tab>
        </TabList>
        <TabPanel value="tickets">
          <p className="text-body text-subtle">The tickets tab.</p>
        </TabPanel>
        <TabPanel value="configuration">
          <p className="text-body text-subtle">The configuration tab.</p>
        </TabPanel>
      </Tabs>
      <SegmentedControl
        label="Order"
        value={order}
        onChange={setOrder}
        options={[
          { value: "newest", label: "Newest first" },
          { value: "oldest", label: "Oldest first", count: 3 },
        ]}
      />
    </Section>
  );
}

export function States() {
  return (
    <Section title="Loading and empty">
      <Row label="spinner">
        <Spinner inline size="small" delay={0} />
        <Spinner inline delay={0} />
        <Spinner inline size="large" delay={0} />
      </Row>
      <Row label="skeleton">
        <Skeleton width="12rem" />
      </Row>
      <SkeletonRows rows={2} columns={4} />
      <EmptyState
        header="No projects yet"
        description="Create one to connect Factorial to a repository."
        primaryAction={<Button appearance="primary">Create project</Button>}
        secondaryAction={<Button>Read the setup guide</Button>}
      />
    </Section>
  );
}

export function CodeAndFigures() {
  const [name, setName] = useState("codex team");
  return (
    <Section title="Code, figures and in-place edits">
      <Row label="code">
        <Code>factorial:blocked</Code>
        <CopyButton value="factorial:blocked" />
      </Row>
      <CodeBlock text={YAML} label="process-simple.yaml" showLineNumbers />
      <Row label="metrics">
        <Metric label="Cost">$ 4.20</Metric>
        <Metric label="Tokens">1.2M tok</Metric>
        <Metric label="Runtime">1h 30m</Metric>
        <Metric label="Retries">2 retries</Metric>
      </Row>
      <Row label="time">
        <RelativeTime iso="2026-09-14T09:12:00Z" />
      </Row>
      <div className="max-w-sm">
        <InlineEditText
          label="Name"
          value={name}
          validate={(value) => (value.trim() ? null : "A runtime needs a name.")}
          onConfirm={setName}
        />
      </div>
      <Panel title="A collapsible panel" count={3} isCollapsible isOpen onToggle={() => {}}>
        <p className="p-4 text-body text-subtle">Folded bodies stay mounted.</p>
      </Panel>
    </Section>
  );
}

export function Overlays() {
  const [open, setOpen] = useState<"drawer" | "modal" | "confirm" | "text" | null>(null);
  return (
    <Section title="Overlays">
      <Row label="layers">
        <Button onClick={() => setOpen("drawer")}>Open drawer</Button>
        <Button onClick={() => setOpen("modal")}>Open modal</Button>
        <Button appearance="danger" onClick={() => setOpen("confirm")}>
          Delete runtime
        </Button>
        <Button onClick={() => setOpen("text")}>Open text drawer</Button>
      </Row>
      <Row label="menus and tips">
        <DropdownMenu
          trigger={{ label: "More actions", icon: "more", iconOnly: true }}
          items={[
            { label: "Rename", onClick: () => {} },
            {
              label: "Duplicate",
              onClick: () => {},
              description: "Copies the settings, not the tickets.",
            },
            {
              group: "Danger",
              items: [
                { label: "Delete", onClick: () => {}, unavailable: "Pause admission first." },
              ],
            },
          ]}
        />
        <Tooltip content="A tooltip on hover and focus">
          <Button>Hover or focus me</Button>
        </Tooltip>
        <ConfirmPopup
          label="Exit"
          name="Exit ticket"
          title="Exit this ticket?"
          iconBefore="exclamation-circle"
          spacing="compact"
          confirmLabel="Exit ticket"
          consequence="The loop stops working on this ticket until it is re-entered."
          resetKey="catalogue"
          onConfirm={() => showFlag({ appearance: "info", title: "Exited" })}
        />
      </Row>
      <Row label="flags">
        {(["success", "info", "warning", "error"] as const).map((appearance) => (
          <Button
            key={appearance}
            spacing="compact"
            onClick={() =>
              showFlag({
                appearance,
                title: `${appearance.charAt(0).toUpperCase()}${appearance.slice(1)} flag`,
                description: "What happened, in a sentence.",
                actions:
                  appearance === "success" ? [{ text: "Undo", onClick: () => {} }] : undefined,
              })
            }
          >
            {appearance}
          </Button>
        ))}
      </Row>
      {open === "drawer" && (
        <Drawer
          title="Board settings"
          subtitle="Shading thresholds and ticket size."
          onClose={() => setOpen(null)}
          footer={
            <Button appearance="primary" onClick={() => setOpen(null)}>
              Done
            </Button>
          }
        >
          <p className="text-body text-subtle">A modal drawer, for editing.</p>
        </Drawer>
      )}
      {open === "modal" && (
        <ModalDialog
          title="Replace with the template?"
          appearance="warning"
          onClose={() => setOpen(null)}
          footer={
            <>
              <Button onClick={() => setOpen(null)}>Cancel</Button>
              <Button appearance="warning" onClick={() => setOpen(null)}>
                Replace
              </Button>
            </>
          }
        >
          Your edits are replaced by the template. You can undo this from the flag.
        </ModalDialog>
      )}
      {open === "confirm" && (
        <ConfirmDialog
          title="Delete runtime codex-team?"
          consequence="This removes codex-team and its stored credentials."
          confirmLabel="Delete runtime"
          onCancel={() => setOpen(null)}
          onConfirm={() => setOpen(null)}
        />
      )}
      {open === "text" && (
        <TextDrawer
          title="Run summary"
          subtitle="spec · attempt 1"
          body="The same drawer, for reading."
          onClose={() => setOpen(null)}
        />
      )}
    </Section>
  );
}

const OUTCOME_LEGEND = [
  { key: "done", look: "outcome-done", label: "Done" },
  { key: "open", look: "outcome-open", label: "Open" },
  { key: "exited", look: "outcome-exited", label: "Exited" },
];

/** Every chart design primitive with the shape of real data: the figures in the hero row, the
 *  parts of a whole, bars that share a max, and a segmented row with its parts written under it. */
export function Figures() {
  return (
    <Section title="Figures">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Spend"
          value="~$66.17"
          caption="lifetime · 17 tickets"
          aside={
            <InlineMessage iconLabel="Why the cost is an estimate" title="Estimated cost">
              At least one attempt ran on a model that reports no price.
            </InlineMessage>
          }
        />
        <StatTile label="Per done ticket" value="$5.80" caption="median $2.81 · 8 done" />
        <StatTile label="Tickets" value="8 done" caption="8 exited · 1 open" />
        <StatTile label="Tokens" value={null} caption="nothing has run yet" />
      </div>
      <StackedBar
        label="Spend by outcome"
        segments={[
          { key: "done", look: "outcome-done", label: "Done", value: 46.4, text: "$46.40 · 70%" },
          { key: "open", look: "outcome-open", label: "Open", value: 11.67, text: "$11.67 · 18%" },
          {
            key: "exited",
            look: "outcome-exited",
            label: "Exited",
            value: 8.09,
            text: "$8.09 · 12%",
            tip: "4 tickets exited before any run",
          },
        ]}
      />
      <Legend label="Outcomes" items={OUTCOME_LEGEND} />
      <BarList
        label="Cost by ticket"
        format={(value) => `$${value.toFixed(2)}`}
        rows={[
          { key: "67", label: "#67 Comments", value: 17.21, text: "$17.21", look: "outcome-done" },
          {
            key: "76",
            label: "#76 Export",
            value: 11.67,
            text: "$11.67",
            look: "outcome-open",
            tip: "#76 open · $11.67 · 2 attempts",
          },
          { key: "57", label: "#57 Dummy", value: 0.17, text: "$0.17", look: "outcome-exited" },
          { key: "95", label: "#95 Search", value: 0, text: "$0.00", look: "outcome-exited" },
        ]}
      />
      <Legend
        label="Steps"
        items={[
          { key: "plan", look: "series-1", label: "plan" },
          { key: "plan-rework", look: "rework-1", label: "plan, rework" },
          { key: "build", look: "series-2", label: "build" },
          { key: "build-rework", look: "rework-2", label: "build, rework" },
          { key: "retry", look: "retry", label: "retry after a failure" },
        ]}
      />
      <BarList
        label="Where the steps spend"
        max={17.21}
        format={(value) => `$${value.toFixed(2)}`}
        rows={[
          {
            key: "76",
            label: "#76 Export",
            value: 11.67,
            text: "$11.67",
            detail: "plan $2.51 · plan, rework $1.58 · build $7.58",
            segments: [
              { key: "plan", look: "series-1", value: 2.51, tip: "plan, attempt 1 · $2.51" },
              { key: "plan-2", look: "rework-1", value: 1.58, tip: "plan, attempt 2 · $1.58" },
              { key: "build", look: "series-2", value: 7.58, tip: "build, attempt 1 · $7.58" },
            ],
          },
          {
            key: "77",
            label: "#77 Billing",
            value: 6.4,
            text: "$6.40",
            detail: "plan $0.93 · build $0.77 · retry $4.70",
            segments: [
              { key: "plan", look: "series-1", value: 0.93 },
              { key: "build", look: "series-2", value: 0.77 },
              {
                key: "build-2",
                look: "retry",
                value: 4.7,
                tip: "build, attempt 2 · retry · $4.70",
              },
            ],
          },
        ]}
      />
    </Section>
  );
}

export function Table() {
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(1);
  const rows = [...ROWS].sort((a, b) => (order === "asc" ? a.id - b.id : b.id - a.id));
  return (
    <Panel title="Table" count={ROWS.length}>
      <DataTable
        label="Example tickets"
        head={[
          { key: "key", content: "Issue", isSortable: true },
          { key: "status", content: "Status" },
          { key: "cost", content: "Cost", align: "end" },
          { key: "updated", content: "Updated" },
        ]}
        rows={rows}
        rowKey={(row) => row.id}
        emptyView={<EmptyState header="No tickets" />}
        sortKey="key"
        sortOrder={order}
        onSort={() => setOrder(order === "asc" ? "desc" : "asc")}
        rowsPerPage={2}
        page={page}
        onSetPage={setPage}
        cells={(row) => ({
          key: <Code>{row.key}</Code>,
          status: <StatusLozenge status={row.status} />,
          cost: row.cost,
          updated: <RelativeTime iso={row.updated} />,
        })}
      />
    </Panel>
  );
}
