/** The catalogue's parts (`pages/CataloguePage.tsx`): the design primitives, one section each. */

import { type ReactNode, useState } from "react";
import { ApiError, SERVER_FAILED } from "../../api/client";
import { CONDITION_TONES, type Role, type TicketCondition } from "../../status";
import { Button, ButtonGroup, IconButton, LinkButton } from "./Button";
import { Code } from "./Code";
import { Checkbox, RadioGroup, Toggle } from "./choices";
import { showFlag } from "./Flag";
import { Form, FormFooter, RequiredLegend } from "./form";
import { GLYPH_NAMES, type Glyph, Icon } from "./icons";
import { Badge, Lozenge, StatusLozenge, Tag, TagGroup } from "./Lozenge";
import { Banner, ErrorMessage, InlineMessage, SectionMessage, StaleNote } from "./messages";
import { Panel } from "./Panel";
import { Select, TextArea, TextField } from "./TextField";

const ROLES: Role[] = ["neutral", "information", "success", "warning", "danger", "discovery"];
const CONDITIONS = Object.keys(CONDITION_TONES) as TicketCondition[];
const APPEARANCES = ["default", "primary", "subtle", "outline", "warning", "danger"] as const;

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Panel title={title}>
      <div className="space-y-4 p-4">{children}</div>
    </Panel>
  );
}

export function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-2 sm:grid-cols-[10rem_1fr] sm:items-center">
      <span className="text-body-sm font-semibold text-subtle">{label}</span>
      <div className="flex flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}

export function Icons() {
  return (
    <Section title="Icons">
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-2">
        {GLYPH_NAMES.map((glyph: Glyph) => (
          <li key={glyph} className="flex items-center gap-2 text-body-sm text-subtle">
            <Icon glyph={glyph} className="text-icon" />
            <Code>{glyph}</Code>
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function Buttons() {
  const [pressed, setPressed] = useState(false);
  return (
    <Section title="Buttons">
      {APPEARANCES.map((appearance) => (
        <Row key={appearance} label={appearance}>
          <Button appearance={appearance}>Save changes</Button>
          <Button appearance={appearance} spacing="compact">
            Compact
          </Button>
          <Button appearance={appearance} spacing="small">
            Small
          </Button>
          <Button appearance={appearance} iconBefore="plus">
            With icon
          </Button>
          <Button appearance={appearance} isLoading>
            Loading
          </Button>
          <Button appearance={appearance} isDisabled>
            Disabled
          </Button>
        </Row>
      ))}
      <Row label="selected">
        <Button isSelected={pressed} onClick={() => setPressed(!pressed)}>
          Toggle me
        </Button>
        <Button appearance="subtle" isSelected>
          Selected subtle
        </Button>
      </Row>
      <Row label="with a reason">
        <Button disabledReason="The worker is offline.">Run now</Button>
      </Row>
      <Row label="links and icons">
        <LinkButton to="/overview">Internal link</LinkButton>
        <LinkButton href="https://atlassian.design/components" appearance="subtle">
          External link
        </LinkButton>
        <IconButton icon="settings" label="Settings" />
        <IconButton icon="more" label="More actions" appearance="default" />
        <IconButton icon="close" label="Close" spacing="compact" />
      </Row>
      <Row label="group">
        <ButtonGroup label="Ticket actions">
          <Button iconBefore="refresh">Refresh</Button>
          <Button appearance="warning">Exit</Button>
        </ButtonGroup>
      </Row>
    </Section>
  );
}

export function Lozenges() {
  return (
    <Section title="Lozenges, badges and tags">
      <Row label="subtle">
        {ROLES.map((role) => (
          <Lozenge key={role} appearance={role}>
            {role}
          </Lozenge>
        ))}
      </Row>
      <Row label="bold">
        {ROLES.map((role) => (
          <Lozenge key={role} appearance={role} isBold>
            {role}
          </Lozenge>
        ))}
      </Row>
      <Row label="ticket conditions">
        {CONDITIONS.map((condition) => (
          <StatusLozenge key={condition} status={condition} />
        ))}
      </Row>
      <Row label="badges">
        <Badge>{4}</Badge>
        <Badge appearance="inverse">{12}</Badge>
        <Badge appearance="danger">{3}</Badge>
        <Badge appearance="information">{140}</Badge>
      </Row>
      <Row label="tags">
        <TagGroup label="Example tags">
          <Tag>codex_api</Tag>
          <Tag to="/runtimes">Linked tag</Tag>
          <Tag onRemove={() => {}} removeLabel="Clear metric">
            Metric: Cost
          </Tag>
        </TagGroup>
      </Row>
    </Section>
  );
}

export function Messages() {
  return (
    <Section title="Messages">
      {(["information", "success", "warning", "error", "discovery"] as const).map((appearance) => (
        <SectionMessage
          key={appearance}
          appearance={appearance}
          title={`${appearance.charAt(0).toUpperCase()}${appearance.slice(1)} message`}
          actions={[
            { text: "Primary action", onClick: () => {} },
            { text: "Learn more", href: "https://atlassian.design" },
          ]}
        >
          What happened, and what to do about it, in one or two sentences.
        </SectionMessage>
      ))}
      <ErrorMessage
        error={new ApiError(502, SERVER_FAILED, null, { detail: "502 Bad Gateway" })}
        title="Couldn't load the projects"
        onRetry={() => {}}
      />
      <Row label="inline">
        <InlineMessage iconLabel="Why this is an estimate" secondaryText="Estimated">
          The run reported tokens but no cost, so the cost uses list prices.
        </InlineMessage>
        <InlineMessage appearance="warning" iconLabel="Why this isn't checked">
          The check needs the App installation.
        </InlineMessage>
        <InlineMessage appearance="error" iconLabel="What failed">
          The last read failed.
        </InlineMessage>
      </Row>
      <Row label="stale">
        <StaleNote updatedAt={Date.now() - 120_000} />
      </Row>
      <Banner appearance="warning" action={{ text: "Reload", onClick: () => {} }}>
        A new version of Factorial is available.
      </Banner>
      <Banner appearance="error">You're offline. Changes won't save.</Banner>
    </Section>
  );
}

export function Fields() {
  const [type, setType] = useState("codex_api");
  const [sandbox, setSandbox] = useState("cloud");
  const [checked, setChecked] = useState(true);
  const [on, setOn] = useState(false);
  return (
    <Section title="Forms">
      <Form onSubmit={() => showFlag({ appearance: "success", title: "Submitted" })}>
        {({ attempted }) => (
          <div className="grid gap-4 md:grid-cols-2">
            <RequiredLegend />
            <span />
            <TextField
              label="Name"
              isRequired
              helperMessage="Shown on the board and in the list."
            />
            <TextField
              label="Slug"
              isRequired
              errorMessage={attempted ? "A slug needs at least one letter." : undefined}
              isMonospaced
            />
            <TextField
              label="Valid"
              defaultValue="acme/widgets"
              validMessage="The App can read it."
            />
            <TextField
              label="Search"
              labelHidden
              placeholder="Search"
              elemBeforeInput={<Icon glyph="search" />}
            />
            <TextField label="Compact" spacing="compact" defaultValue="compact" />
            <TextField label="Read-only" readOnly defaultValue="local_cli" />
            <Select
              label="Runtime"
              options={[
                { value: "a", label: "codex-team" },
                { value: "b", label: "claude-key" },
              ]}
            />
            <TextArea label="Description" helperMessage="Markdown is kept as written." />
            <RadioGroup
              label="Type"
              name="catalogue-type"
              value={type}
              onChange={setType}
              options={[
                {
                  value: "codex_api",
                  label: "Codex · API key",
                  description: "Signs in with a key.",
                },
                { value: "codex_subscription", label: "Codex · subscription" },
                { value: "off", label: "Unavailable", isDisabled: true },
              ]}
            />
            <div className="md:col-span-2">
              <RadioGroup
                label="Sandbox"
                name="catalogue-sandbox"
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
                  {
                    value: "elsewhere",
                    label: "Not offered",
                    description: "A choice this installation leaves out.",
                    isDisabled: true,
                    disabledReason: "Not available on this installation",
                  },
                ]}
              />
            </div>
            <div className="flex flex-col items-start gap-2">
              <Checkbox label="Include closed issues" isChecked={checked} onChange={setChecked} />
              <Checkbox
                label="Some of these"
                isChecked={false}
                isIndeterminate
                onChange={() => {}}
              />
              <Checkbox label="Disabled" isChecked={false} isDisabled onChange={() => {}} />
              <Toggle label="Admission" isChecked={on} onChange={setOn} />
              <Toggle label="Loading" isChecked isLoading onChange={() => {}} />
            </div>
            <FormFooter align="end">
              <Button appearance="subtle">Cancel</Button>
              <Button appearance="primary" type="submit">
                Create
              </Button>
            </FormFooter>
          </div>
        )}
      </Form>
    </Section>
  );
}
