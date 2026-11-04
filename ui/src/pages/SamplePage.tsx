/**
 * The sample reference page (docs/design-system.md, Reference pages). It is also rendered to a static file held as a
 * snapshot, so it holds no clock, no query and no open portal: what it draws must be the same on
 * every render.
 */

import { useState } from "react";
import {
  BarList,
  Button,
  Checkbox,
  Code,
  CodeBlock,
  DataTable,
  EmptyState,
  FormFooter,
  IconButton,
  Lozenge,
  NavLinkItem,
  PageHeader,
  Panel,
  RadioGroup,
  SectionMessage,
  Select,
  StackedBar,
  StatTile,
  StatusLozenge,
  Tab,
  TabList,
  TabPanel,
  Tabs,
  Tag,
  TagGroup,
  TextField,
  Toggle,
  usePageTitle,
} from "../components/ui";
import { ReferenceNav } from "./ReferenceNav";

const PROCESS = `specVersion: factorial/v1
kind: Process
steps:
  - name: spec
    agent: spec-writer
    next: spec_gate`;

type Ticket = {
  id: number;
  issue: string;
  title: string;
  status: string;
  step: string;
  cost: string;
};

const TICKETS: Ticket[] = [
  {
    id: 42,
    issue: "acme/widgets#42",
    title: "Export the board as CSV",
    status: "running",
    step: "build",
    cost: "$4.20",
  },
  {
    id: 57,
    issue: "acme/widgets#57",
    title: "Rate-limit the webhook",
    status: "waiting_gate",
    step: "spec_gate",
    cost: "$1.05",
  },
  {
    id: 61,
    issue: "acme/widgets#61",
    title: "Dark mode for the settings page",
    status: "rejected",
    step: "spec",
    cost: "$0.88",
  },
  {
    id: 7,
    issue: "acme/widgets#7",
    title: "Migrate the cron jobs",
    status: "blocked",
    step: "build",
    cost: "$12.90",
  },
  {
    id: 3,
    issue: "acme/widgets#3",
    title: "Add a health check",
    status: "done",
    step: "done",
    cost: "$2.31",
  },
];

const NAV = [
  { to: "/sample", icon: "overview", label: "Overview", isCurrent: true },
  { to: "/sample/tickets", icon: "tickets", label: "Tickets" },
  { to: "/sample/runtimes", icon: "runtimes", label: "Runtimes" },
  { to: "/sample/agents", icon: "agent", label: "Agents" },
  { to: "/sample/settings", icon: "settings", label: "Settings" },
] as const;

function TopBar() {
  return (
    <header className="flex min-h-14 flex-wrap items-center gap-3 border-b border-default bg-surface px-4 py-2">
      <span className="text-heading-sm">Factorial</span>
      <Lozenge appearance="discovery">Sample</Lozenge>
      <span className="ml-auto flex flex-wrap items-center gap-1">
        <ReferenceNav current="sample" />
        <IconButton icon="search" label="Search" />
        <IconButton icon="help" label="Help" />
        <IconButton icon="user" label="Account" />
      </span>
    </header>
  );
}

function SideNav() {
  return (
    <nav
      aria-label="Sample navigation"
      className="hidden w-60 shrink-0 border-r border-default bg-surface p-2 md:block"
    >
      <ul className="space-y-0.5">
        {NAV.map((item) => (
          <li key={item.to}>
            <NavLinkItem
              to={item.to}
              icon={item.icon}
              label={item.label}
              isCurrent={"isCurrent" in item}
            />
          </li>
        ))}
      </ul>
    </nav>
  );
}

function Figures() {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatTile label="Open tickets" value="4" caption="2 need a person" />
      <StatTile label="Spend" value="$21.34" caption="this month · 5 tickets" />
      <StatTile label="Per done ticket" value="$2.31" caption="median · 1 done" />
      <StatTile label="Tokens" value={null} caption="nothing has run today" />
    </div>
  );
}

function TicketTable() {
  const [tab, setTab] = useState("open");
  const open = TICKETS.filter((ticket) => ticket.status !== "done");
  const table = (rows: Ticket[]) => (
    <DataTable
      label="Tickets"
      head={[
        { key: "issue", content: "Issue" },
        { key: "title", content: "Title" },
        { key: "step", content: "Step" },
        { key: "status", content: "Status" },
        { key: "cost", content: "Cost", align: "end" },
      ]}
      rows={rows}
      rowKey={(row) => row.id}
      emptyView={<EmptyState header="No tickets" headingLevel={3} width="narrow" />}
      cells={(row) => ({
        issue: <Code>{row.issue}</Code>,
        title: row.title,
        step: row.step,
        status: <StatusLozenge status={row.status} />,
        cost: row.cost,
      })}
    />
  );
  return (
    <Panel
      title="Tickets"
      count={TICKETS.length}
      actions={<Button spacing="compact">Export</Button>}
    >
      <Tabs id="sample-tickets" selected={tab} onChange={setTab}>
        <div className="px-4">
          <TabList label="Ticket filter">
            <Tab value="open" badge={open.length}>
              Open
            </Tab>
            <Tab value="all">All</Tab>
          </TabList>
        </div>
        <TabPanel value="open">{table(open)}</TabPanel>
        <TabPanel value="all">{table(TICKETS)}</TabPanel>
      </Tabs>
    </Panel>
  );
}

function Spend() {
  return (
    <Panel title="Spend by outcome" subtitle="This month">
      <div className="space-y-4 p-4">
        <StackedBar
          label="Spend by outcome"
          segments={[
            { key: "done", look: "outcome-done", label: "Done", value: 2.31, text: "$2.31 · 11%" },
            {
              key: "open",
              look: "outcome-open",
              label: "Open",
              value: 18.15,
              text: "$18.15 · 85%",
            },
            {
              key: "exited",
              look: "outcome-exited",
              label: "Exited",
              value: 0.88,
              text: "$0.88 · 4%",
            },
          ]}
        />
        <BarList
          label="Cost by ticket"
          format={(value) => `$${value.toFixed(2)}`}
          rows={[
            { key: "7", label: "#7 Cron jobs", value: 12.9, text: "$12.90", look: "outcome-open" },
            { key: "42", label: "#42 CSV export", value: 4.2, text: "$4.20", look: "outcome-open" },
            {
              key: "3",
              label: "#3 Health check",
              value: 2.31,
              text: "$2.31",
              look: "outcome-done",
            },
            {
              key: "57",
              label: "#57 Rate limit",
              value: 1.05,
              text: "$1.05",
              look: "outcome-open",
            },
          ]}
        />
      </div>
    </Panel>
  );
}

function RuntimeForm() {
  const [sandbox, setSandbox] = useState("cloud");
  const [admission, setAdmission] = useState(true);
  const [closed, setClosed] = useState(false);
  return (
    <Panel title="Add a runtime" subtitle="A form, with its helper, error and valid messages">
      <form className="grid gap-4 p-4 md:grid-cols-2" onSubmit={(event) => event.preventDefault()}>
        <TextField label="Name" isRequired helperMessage="Shown on the board and in the list." />
        <TextField
          label="Slug"
          isRequired
          isMonospaced
          defaultValue="-"
          errorMessage="A slug needs at least one letter."
        />
        <Select
          label="Adapter"
          options={[
            { value: "claude_cli", label: "claude_cli" },
            { value: "codex_cli", label: "codex_cli" },
          ]}
        />
        <TextField
          label="Repository"
          defaultValue="acme/widgets"
          validMessage="The App can read it."
        />
        <div className="md:col-span-2">
          <RadioGroup
            label="Sandbox"
            name="sample-sandbox"
            appearance="tile"
            value={sandbox}
            onChange={setSandbox}
            options={[
              {
                value: "cloud",
                label: "Managed cloud sandbox",
                description: "A fresh sandbox per run. Keys stay outside it.",
              },
              {
                value: "host",
                label: "On the host",
                description: "The agent can read the host's files.",
                lozenge: { text: "Not recommended for prod", appearance: "warning" },
              },
            ]}
          />
        </div>
        <div className="flex flex-col items-start gap-2">
          <Toggle label="Admission" isChecked={admission} onChange={setAdmission} />
          <Checkbox label="Include closed issues" isChecked={closed} onChange={setClosed} />
        </div>
        <FormFooter align="end">
          <Button appearance="subtle">Cancel</Button>
          <Button appearance="primary" type="submit">
            Add runtime
          </Button>
        </FormFooter>
      </form>
    </Panel>
  );
}

export function SampleContent() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="acme/widgets"
        titleAfter={<StatusLozenge status="running" />}
        actions={
          <>
            <Button appearance="subtle" iconBefore="refresh">
              Refresh
            </Button>
            <Button appearance="primary" iconBefore="plus">
              New ticket
            </Button>
          </>
        }
        bottomBar={
          <TagGroup label="Project facts">
            <Tag>claude_cli</Tag>
            <Tag>process-simple v3</Tag>
            <Tag>main protected</Tag>
          </TagGroup>
        }
      />
      <SectionMessage
        appearance="warning"
        title="Two tickets need a person"
        actions={[{ text: "Open the gate", href: "#tickets" }]}
      >
        <Code>acme/widgets#57</Code> waits at the spec gate, and <Code>acme/widgets#7</Code> is
        blocked after three failed attempts.
      </SectionMessage>
      <Figures />
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="min-w-0 space-y-6 lg:col-span-2">
          <TicketTable />
          <Spend />
          <RuntimeForm />
        </div>
        <div className="min-w-0 space-y-6">
          <Panel title="Process">
            <div className="p-4">
              <CodeBlock text={PROCESS} label="process-simple.yaml" showLineNumbers />
            </div>
          </Panel>
          <Panel title="Agents">
            <EmptyState
              header="No agents yet"
              description="Add one under .factorial/agents/ in the repository."
              headingLevel={3}
              width="narrow"
              primaryAction={<Button>Read the guide</Button>}
            />
          </Panel>
        </div>
      </div>
    </div>
  );
}

/** The page inside a shell of its own: the real `AppShell` needs a session and an organization. */
export default function SamplePage() {
  usePageTitle(["Sample"]);
  return (
    <div className="flex min-h-dvh flex-col bg-surface-sunken">
      <TopBar />
      <div className="flex flex-1">
        <SideNav />
        <main className="min-w-0 flex-1 px-4 py-6 lg:px-8">
          <SampleContent />
        </main>
      </div>
    </div>
  );
}
