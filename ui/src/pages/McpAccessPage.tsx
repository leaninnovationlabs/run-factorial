/**
 * MCP access: the tokens Claude Code or Codex reach the factorial MCP server with, a way to make
 * one, how to set a client up, and the tools a token reaches.
 *
 * A page, not a drawer: the side navigation unmounts below 768px, and a drawer opened from it
 * would take a shown-once token with it. The token lives only in the mint mutation's result,
 * which is dropped when `NewToken` unmounts — on Done, on Cancel, or on leaving the page
 * (`useMintMcpToken`) — never in a flag, the title, storage or the console.
 */

import { useEffect, useId, useRef, useState } from "react";
import { useMcpTokens, useMcpTools, useMintMcpToken } from "../api/queries";
import type { McpMinter, McpTokenMinted, McpTokenMintIn, McpTool } from "../api/types";
import { useAuth } from "../auth";
import { claudeSnippet, codexSnippet } from "../components/account/setupSnippets";
import { AbsoluteTime, ACCESS_LABELS, TokenTable } from "../components/account/TokenTable";
import {
  Button,
  Checkbox,
  Code,
  CodeBlock,
  CopyButton,
  ErrorMessage,
  Form,
  FormFooter,
  Lozenge,
  PageHeader,
  Panel,
  QueryBoundary,
  SectionMessage,
  staleSince,
  Tab,
  TabList,
  TabPanel,
  Tabs,
  TextField,
  useActionFeedback,
  usePageTitle,
} from "../components/ui";

export default function McpAccessPage() {
  const tokens = useMcpTokens();
  const tokensHeading = useRef<HTMLSpanElement>(null);
  const newTokenButton = useRef<HTMLButtonElement>(null);
  const [isComposing, setComposing] = useState(false);
  const returnFocus = useRef(false);
  usePageTitle(["MCP access"]);

  // The button is not drawn while the form is open, so its focus is given back once it is.
  useEffect(() => {
    if (isComposing || !returnFocus.current) return;
    returnFocus.current = false;
    newTokenButton.current?.focus();
  }, [isComposing]);

  function close() {
    returnFocus.current = true;
    setComposing(false);
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="MCP access"
        actions={
          tokens.data &&
          !isComposing && (
            <Button
              ref={newTokenButton}
              appearance="primary"
              data-assist-anchor="mcp.new_token"
              onClick={() => setComposing(true)}
            >
              New token
            </Button>
          )
        }
        bottomBar={
          tokens.data && (
            <span className="flex flex-wrap items-center gap-2">
              Server <Code>{tokens.data.mcp_url}</Code>
              <CopyButton value={tokens.data.mcp_url} name="server address" />
            </span>
          )
        }
        staleSince={staleSince(tokens)}
      />

      <QueryBoundary query={tokens} errorTitle="Couldn't load your MCP tokens">
        {(index) => (
          <>
            {isComposing && (
              <Panel>
                <div className="p-4">
                  <NewToken
                    minter={index.minter}
                    url={index.mcp_url}
                    isChecking={tokens.isFetching}
                    onCheckAgain={() => tokens.refetch()}
                    onClose={close}
                  />
                </div>
              </Panel>
            )}
            <Panel
              title={
                <span ref={tokensHeading} tabIndex={-1} className="rounded-xs">
                  Tokens
                </span>
              }
              count={index.tokens.length}
            >
              <TokenTable tokens={index.tokens} headingRef={tokensHeading} />
            </Panel>
            <ConnectClient url={index.mcp_url} />
          </>
        )}
      </QueryBoundary>

      <ToolCatalog />
    </div>
  );
}

/** The first that holds wins: a token just made, a sign-in too old to mint, then the form. */
function NewToken({
  minter,
  url,
  isChecking,
  onCheckAgain,
  onClose,
}: {
  minter: McpMinter;
  url: string;
  isChecking: boolean;
  onCheckAgain: () => void;
  onClose: () => void;
}) {
  const mint = useMintMcpToken();
  useActionFeedback(mint, { errorTitle: "Couldn't create the token" });
  const { reset, isError } = mint;
  // The stale-sign-in refusal is answered by the prompt below; once a fresh sign-in clears it,
  // the form must not come back still showing that refusal.
  useEffect(() => {
    if (minter.reauth_required && isError) reset();
  }, [minter.reauth_required, isError, reset]);

  if (mint.data) return <ShownOnce minted={mint.data} url={url} onDone={onClose} />;
  if (minter.reauth_required) {
    return <SignInAgain isChecking={isChecking} onCheckAgain={onCheckAgain} onCancel={onClose} />;
  }
  return (
    <MintForm
      isPending={mint.isPending}
      error={mint.error}
      onMint={(body) => mint.mutate(body)}
      onCancel={onClose}
    />
  );
}

function ShownOnce({
  minted,
  url,
  onDone,
}: {
  minted: McpTokenMinted;
  url: string;
  onDone: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  const { row } = minted;
  return (
    <div className="space-y-3">
      <h3 ref={heading} tabIndex={-1} className="rounded-xs text-heading-xs text-default">
        Your new token
      </h3>
      <SectionMessage appearance="warning">
        This token is shown only once. Copy it now and save it in your password manager.
      </SectionMessage>
      <CodeBlock text={minted.token} label="MCP token" wrap className="[&_pre]:select-all" />
      <p className="text-body-sm text-subtle">
        {row.label} · {ACCESS_LABELS[row.access]} · expires <AbsoluteTime iso={row.expires_at} />
      </p>
      <ConnectClient url={url} isNested />
      <Button onClick={onDone}>Done</Button>
    </div>
  );
}

export function SignInAgain({
  isChecking,
  onCheckAgain,
  onCancel,
}: {
  isChecking: boolean;
  onCheckAgain: () => void;
  onCancel: () => void;
}) {
  const { signInAgain } = useAuth();
  return (
    <div className="space-y-3">
      <SectionMessage appearance="warning" title="Sign in again to create a token">
        A token lasts a year, so you must have signed in within the last 24 hours to create one.
        Press Sign in again and sign in. This page opens again after that. If you signed in on
        another tab, press Check again.
      </SectionMessage>
      <div className="flex flex-wrap gap-2">
        <Button appearance="primary" onClick={() => void signInAgain()}>
          Sign in again
        </Button>
        <Button isLoading={isChecking} onClick={onCheckAgain}>
          Check again
        </Button>
        <Button onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

function MintForm({
  isPending,
  error,
  onMint,
  onCancel,
}: {
  isPending: boolean;
  error: unknown;
  onMint: (body: McpTokenMintIn) => void;
  onCancel: () => void;
}) {
  const [label, setLabel] = useState("");
  const [canWrite, setCanWrite] = useState(false);
  const labelField = useRef<HTMLInputElement>(null);
  useEffect(() => {
    labelField.current?.focus();
  }, []);
  const labelProblem =
    label.trim() === "" ? "Name it after the client or machine that will use it." : null;

  return (
    <Form
      aria-label="New MCP token"
      validate={() => !labelProblem}
      onSubmit={() => onMint({ label: label.trim(), access: canWrite ? "write" : "read" })}
      className="space-y-4"
    >
      {({ attempted }) => (
        <>
          <TextField
            ref={labelField}
            label="Label"
            isRequired
            maxLength={80}
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="Claude Code on my laptop"
            errorMessage={attempted ? (labelProblem ?? undefined) : undefined}
          />
          <div className="space-y-2">
            <fieldset className="space-y-2">
              <legend className="text-body-sm font-semibold text-subtle">Access</legend>
              <AccessChoice
                label="Read"
                description="List projects and tickets, read metrics and config."
                isChecked
                isDisabled
                onChange={() => {}}
              />
              <AccessChoice
                label="Write"
                description="Create tickets, act on them, and commit config."
                isChecked={canWrite}
                onChange={setCanWrite}
              />
            </fieldset>
            <p className="text-body-sm text-subtle">
              Expires one year from now. You see the token only once, right after you generate it.
            </p>
          </div>
          <ErrorMessage error={error} />
          <FormFooter>
            <Button type="submit" appearance="primary" isLoading={isPending}>
              Generate
            </Button>
            <Button onClick={onCancel}>Cancel</Button>
          </FormFooter>
        </>
      )}
    </Form>
  );
}

function AccessChoice({
  label,
  description,
  isChecked,
  isDisabled = false,
  onChange,
}: {
  label: string;
  description: string;
  isChecked: boolean;
  isDisabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="space-y-0.5">
      <Checkbox label={label} isChecked={isChecked} isDisabled={isDisabled} onChange={onChange} />
      <p className="pl-5.5 text-body-sm text-subtlest">{description}</p>
    </div>
  );
}

/** Folded by default. Inside the shown-once panel it is a heading level down and has no card of
 *  its own. No snippet carries the token (`setupSnippets`). */
function ConnectClient({ url, isNested = false }: { url: string; isNested?: boolean }) {
  const [isOpen, setOpen] = useState(false);
  const [client, setClient] = useState("claude");
  const tabsId = useId();
  return (
    <Panel
      title="Connect a client"
      headingLevel={isNested ? 3 : 2}
      bare={isNested}
      isCollapsible
      isOpen={isOpen}
      onToggle={() => setOpen((open) => !open)}
    >
      <div className={isNested ? "pt-2" : "p-4"}>
        <Tabs id={tabsId} selected={client} onChange={setClient}>
          <TabList label="Client">
            <Tab value="claude">Claude Code</Tab>
            <Tab value="codex">Codex</Tab>
          </TabList>
          <TabPanel value="claude">
            <CodeBlock text={claudeSnippet(url)} label="Add the server to Claude Code" wrap />
          </TabPanel>
          <TabPanel value="codex">
            <CodeBlock text={codexSnippet(url)} label="Add the server to Codex" wrap />
          </TabPanel>
        </Tabs>
      </div>
    </Panel>
  );
}

function ToolCatalog() {
  const tools = useMcpTools();
  const [isOpen, setOpen] = useState(false);
  const all = tools.data?.tools;
  const read = all?.filter((tool) => tool.scope === "factorial:read") ?? [];
  const write = all?.filter((tool) => tool.scope === "factorial:write") ?? [];
  return (
    <Panel
      title={
        all ? `${all.length} tools (${read.length} read, ${write.length} write)` : "Tools available"
      }
      isCollapsible
      isOpen={isOpen}
      onToggle={() => setOpen((open) => !open)}
    >
      <div className="p-4">
        <QueryBoundary query={tools} errorTitle="Couldn't load the tool list">
          {() => (
            <div className="space-y-4">
              <p className="text-body text-subtle">
                A read-only token sees only the read tools. No token can approve or merge at a gate,
                or reach secrets, permits, runtimes or the terminal.
              </p>
              <ToolGroup title="Read" tools={read} />
              <ToolGroup title="Write" tools={write} />
            </div>
          )}
        </QueryBoundary>
      </div>
    </Panel>
  );
}

function ToolGroup({ title, tools }: { title: string; tools: McpTool[] }) {
  return (
    <section className="space-y-2">
      <h3 className="text-heading-xs text-default">{title}</h3>
      <ul className="space-y-1.5">
        {tools
          .toSorted((a, b) => a.name.localeCompare(b.name))
          .map((tool) => (
            <li key={tool.name} className="flex flex-wrap items-baseline gap-x-2 text-body">
              <Code>{tool.name}</Code>
              <span className="text-subtle">{tool.title ?? tool.description}</span>
              {tool.destructive && <Lozenge appearance="warning">Destructive</Lozenge>}
            </li>
          ))}
      </ul>
    </section>
  );
}
