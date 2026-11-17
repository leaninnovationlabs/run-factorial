/**
 * The assistant's conversation: the Factorial mark and the title, with New chat and the
 * conversation history, in its header; the conversation in its body, which scrolls on its own;
 * and the composer at its foot. Four frames for the same parts: the whole of the assistant's page
 * (`AssistantPage`), where the conversation and the composer keep to a reading width down the
 * middle; the modal `Drawer` (decision 50) that opens over a project's pages, from the
 * project's navigation; and the two the Drawer's Dock and Float take the conversation to, which
 * leave the page usable and draw the text at the size the reader chose, without avatars: the
 * column docked beside the page (`DockPanel`), and the small window floating over it
 * (`MiniWindow`), which has no room for the history.
 *
 * The Drawer's header also has Minimize, which every frame has and which puts the conversation
 * away as a bar in the page's corner, and Maximize, which takes the Drawer to its full width and
 * lifts the conversation's reading width with it. Over the composer, a line names the project a
 * message is about (`AskingAbout`).
 *
 * It opens on the conversation the provider holds, or the most recent one. A message is sent with
 * the dashboard page the reader came from as its context (`AssistantSession.contextPage`), and a conversation with none yet is made by the first
 * message, which also gives it its title. The history deletes any conversation, and a deleted one
 * leaves it at once, back only if the API refuses. Only deleting the open one moves the Drawer on,
 * to the next not being deleted, and at the press rather than on the API's answer: a second
 * delete made before the first is answered would otherwise strand it on the first. When the
 * API's `ready_to_send` says a message would be refused (off, or no way to sign in: no stored
 * credential, or no Bedrock token from the host's role), the foot says which, in the API's own
 * sentence, in place of the composer, and links to its settings for a reader who may change them; what was said before
 * stays readable. Not `credential_set`: Bedrock through the host's role stores none.
 *
 * A send the API refuses because a turn is already under way here (another tab's) keeps the
 * composer waiting until the stream shows that turn end: the stream is what says when it is over.
 */

import { useEffect, useRef, useState } from "react";
import logoUrl from "../../../logo.svg";
import { ApiError } from "../../api/client";
import { useCan } from "../../api/organizationQueries";
import {
  useAssistantReady,
  useCancelTurn,
  useConversations,
  useCreateConversation,
  useDeleteConversation,
  useRenameConversation,
  useSendMessage,
} from "../../api/queries";
import type { ChatPointed } from "../../api/types";
import { Drawer, ErrorMessage, IconButton, SectionMessage } from "../ui";
import { useIsPhone } from "../ui/media";
import { AskingAbout } from "./AskingAbout";
import { type AssistantSession, DashboardLink, useAssistant } from "./AssistantProvider";
import { Composer } from "./Composer";
import { ConversationSwitcher } from "./ConversationSwitcher";
import { DockPanel } from "./DockPanel";
import type { Draft, Pointed } from "./draft";
import { TextStepContext } from "./frames";
import { ItemStream } from "./ItemStream";
import { MiniWindow } from "./MiniWindow";
import { isBlank, isLive } from "./useConversationStream";
import { ViewOptions } from "./ViewOptions";

const TITLE_MAX = 80;

/** What was pointed at, as the API takes it: nothing that is not there. */
function told(pointed: Pointed): ChatPointed {
  const { label, text, page, href, anchor, file, lines } = pointed;
  return {
    label,
    text,
    ...(page ? { page } : {}),
    ...(href ? { href } : {}),
    ...(anchor ? { anchor } : {}),
    ...(file ? { file } : {}),
    ...(lines ? { lines } : {}),
  };
}

function titleFrom(text: string): string {
  const line = text.trim().split("\n", 1)[0]?.trim() ?? "";
  return line.length > TITLE_MAX ? `${line.slice(0, TITLE_MAX - 1)}…` : line;
}

export function AssistantChat({
  session,
  variant = "page",
  onClose = () => {},
}: {
  /** What the provider holds for every frame. */
  session: AssistantSession;
  /** `page`: the whole of the assistant's page. `drawer`: the modal Drawer over a project's page.
   *  `dock`: the column beside it. `mini`: the small window floating over it. */
  variant?: "page" | "drawer" | "dock" | "mini";
  onClose?: () => void;
}) {
  const { conversationId, onConversation, scrollTop, contextPage, draft, setDraft } = session;
  const { isWide, setWide, stream, startsNew, wantsFocus, pointedCount } = session;
  const assistant = useAssistant();
  const phone = useIsPhone();
  const beside = variant === "mini" || variant === "dock";
  const settings = useAssistantReady();
  const canConfigure = useCan("assistant:configure");
  const conversations = useConversations();
  const create = useCreateConversation();
  const rename = useRenameConversation();
  const remove = useDeleteConversation();
  const message = useSendMessage();
  const cancel = useCancelTurn();
  const composer = useRef<HTMLTextAreaElement>(null);
  const [focusAsked, askFocus] = useState(0);
  /** The turn the stream showed when the API said another was under way. */
  const [refusedAt, setRefusedAt] = useState<{ conversation: number; turn: number } | null>(null);
  /** The settings as they stood when a send was refused as not set up, and the API's sentence:
   *  the refusal holds until they change. */
  const [refusedWith, setRefusedWith] = useState<{ settings: unknown; reason: string } | null>(
    null,
  );
  /** Deletes not yet answered: out of the history at once, and back in it if the API refuses. */
  const [deleting, setDeleting] = useState<ReadonlySet<number>>(() => new Set());
  /** Conversations made here, untitled until their first message names them. */
  const madeHere = useRef(new Set<number>());

  const picked = useRef(conversationId !== null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `startsNew` is a ref the provider holds
  useEffect(() => {
    // A conversation the person asked to start is the empty one, not the latest.
    if (picked.current || startsNew.current || !conversations.data) return;
    picked.current = true;
    const latest = conversations.data[0];
    if (latest) onConversation(latest.id);
  }, [conversations.data, onConversation]);

  useEffect(() => {
    if (focusAsked > 0) composer.current?.focus();
  }, [focusAsked]);
  // Something was pointed at: the chip is in the composer, and the person writes next. On mount
  // too, for a frame the pointing opened.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the count is the event; the ref says whether it is still owed
  useEffect(() => {
    if (!wantsFocus.current) return;
    wantsFocus.current = false;
    composer.current?.focus();
  }, [pointedCount]);

  const configured = settings.data;
  const refusal = message.error instanceof ApiError ? message.error.code : null;
  const notSetUp =
    refusedWith !== null && refusedWith.settings === configured
      ? refusedWith.reason
      : configured !== undefined && !configured.ready_to_send
        ? (configured.refusal ?? "It is off, or it has no way to sign in yet.")
        : null;
  const blocked =
    refusedAt !== null &&
    refusedAt.conversation === conversationId &&
    (stream.turn?.id ?? 0) <= refusedAt.turn;
  /** A refused Stop is true only while the turn it was for still runs. */
  const stopError =
    isLive(stream.turn) && cancel.variables === stream.turn.id ? cancel.error : null;

  function open(id: number | null) {
    scrollTop.current = null;
    // Whatever opens a conversation settles which one: the latest is not looked for after it.
    picked.current = true;
    if (id !== null) startsNew.current = false;
    onConversation(id);
  }

  async function send(
    { text, pointed, files }: Draft,
    page: string | null = contextPage.current,
  ): Promise<boolean> {
    let id = conversationId;
    try {
      if (id === null) {
        id = (await create.mutateAsync()).id;
        madeHere.current.add(id);
        open(id);
      }
      await message.mutateAsync({
        id,
        text,
        page,
        context: pointed.map(told),
        attachments: files.map(({ name, data }) => ({ name, data })),
      });
    } catch (error) {
      if (id !== null && error instanceof ApiError && error.code === "turn_live") {
        setRefusedAt({ conversation: id, turn: stream.turn?.id ?? 0 });
      }
      if (error instanceof ApiError && error.code === "not_configured") {
        setRefusedWith({ settings: configured, reason: error.message });
        void settings.refetch();
      }
      return false;
    }
    const sent = id;
    // Named only when it is known to have no name: by the list, or because it was made here.
    const listed = conversations.data?.find((conversation) => conversation.id === sent);
    if (listed ? !listed.title : madeHere.current.has(sent)) {
      madeHere.current.delete(sent);
      const named = text || pointed[0]?.label || files[0]?.name || "";
      rename.mutate({ id: sent, title: titleFrom(named) });
    }
    return true;
  }

  const switcher = (
    <ConversationSwitcher
      conversations={conversations.data?.filter((conversation) => !deleting.has(conversation.id))}
      currentId={conversationId}
      isCreating={create.isPending}
      spacing={beside ? "compact" : "default"}
      onSelect={open}
      onNew={() => {
        // Nothing said here yet, or no conversation yet: this is the new chat already, and
        // another press must not leave an empty conversation behind it in the history.
        if (conversationId === null || isBlank(stream)) {
          startsNew.current = conversationId === null;
          picked.current = true;
          askFocus((asked) => asked + 1);
          return;
        }
        create.mutate(undefined, {
          onSuccess: (made) => {
            madeHere.current.add(made.id);
            open(made.id);
            askFocus((asked) => asked + 1);
          },
        });
      }}
      onDelete={(id) => {
        setDeleting((ids) => new Set(ids).add(id));
        if (id === conversationId) {
          open(
            conversations.data?.find(
              (conversation) => conversation.id !== id && !deleting.has(conversation.id),
            )?.id ?? null,
          );
        }
        remove.mutateAsync(id).catch(() =>
          setDeleting((ids) => {
            const left = new Set(ids);
            left.delete(id);
            return left;
          }),
        );
      }}
    />
  );
  const conversation = (
    <>
      {conversations.error && !conversations.data ? (
        <div className="px-5 py-4">
          <ErrorMessage
            error={conversations.error}
            title="Couldn't load your conversations"
            onRetry={() => conversations.refetch()}
          />
        </div>
      ) : stream.gone ? (
        <div className="px-5 py-4">
          <SectionMessage appearance="warning" title="Can't open this conversation">
            It may have been deleted in another tab. Start a new chat, or choose another from your
            conversation history.
          </SectionMessage>
        </div>
      ) : (
        <ItemStream
          key={conversationId ?? "new"}
          items={stream.items}
          turn={stream.turn}
          onRetry={(text, page, pointed) => void send({ text, pointed, files: [] }, page)}
          scrollTop={scrollTop}
          wide={variant === "drawer" && isWide}
          dense={beside}
        />
      )}
    </>
  );
  const foot = notSetUp ? (
    <SectionMessage appearance="warning" title="Set up the assistant">
      {notSetUp}{" "}
      {canConfigure ? (
        <DashboardLink to="/settings">Open the assistant settings</DashboardLink>
      ) : (
        "Ask an admin to set it up."
      )}
    </SectionMessage>
  ) : (
    <div className="space-y-2">
      <AskingAbout page={contextPage.current} />
      <Composer
        inputRef={composer}
        draft={draft}
        onDraft={setDraft}
        compact={beside}
        acceptsImages={configured?.accepts_images !== false}
        turn={stream.turn}
        isBlocked={blocked}
        error={
          create.error ??
          (refusal === "turn_live" || refusal === "not_configured" ? null : message.error) ??
          stopError ??
          remove.error
        }
        onSend={(written) => send(written)}
        onStop={(turnId) => cancel.mutateAsync(turnId)}
      />
    </div>
  );

  if (variant === "drawer") {
    return (
      <Drawer
        title={
          <span className="flex items-center gap-2">
            <img src={logoUrl} alt="" className="size-5 rounded-xs" />
            Factorial Assistant
          </span>
        }
        width={isWide ? "full" : "medium"}
        bare
        initialFocusRef={composer}
        onClose={onClose}
        actions={
          <>
            {switcher}
            {!phone && (
              <>
                {assistant.canDock && (
                  <IconButton icon="dock" label="Dock to the side" onClick={assistant.dock} />
                )}
                <IconButton icon="float" label="Float" onClick={assistant.float} />
                <IconButton icon="minimize" label="Minimize" onClick={assistant.minimize} />
                <IconButton
                  icon="maximize"
                  label={isWide ? "Restore width" : "Maximize"}
                  onClick={() => setWide(!isWide)}
                />
              </>
            )}
          </>
        }
        footer={foot}
      >
        {conversation}
      </Drawer>
    );
  }

  const besideActions = (
    <>
      <IconButton icon="minimize" label="Minimize" spacing="compact" onClick={assistant.minimize} />
      <IconButton
        icon="maximize"
        label="Open in the drawer"
        spacing="compact"
        onClick={assistant.openDrawer}
      />
      <IconButton icon="close" label="Close" spacing="compact" onClick={onClose} />
    </>
  );

  if (variant === "dock") {
    return (
      <TextStepContext.Provider value={assistant.prefs.text}>
        <DockPanel
          width={assistant.prefs.dock}
          onResize={(dock) => assistant.setPrefs({ dock })}
          actions={
            <>
              {switcher}
              <ViewOptions />
              <IconButton icon="float" label="Float" spacing="compact" onClick={assistant.float} />
              {besideActions}
            </>
          }
          footer={foot}
        >
          {conversation}
        </DockPanel>
      </TextStepContext.Provider>
    );
  }

  if (variant === "mini") {
    return (
      <TextStepContext.Provider value={assistant.prefs.text}>
        <MiniWindow
          size={assistant.prefs.mini}
          onResize={(mini) => assistant.setPrefs({ mini })}
          before={<ViewOptions windowSize />}
          after={
            <>
              {assistant.canDock && (
                <IconButton
                  icon="dock"
                  label="Dock to the side"
                  spacing="compact"
                  onClick={assistant.dock}
                />
              )}
              {besideActions}
            </>
          }
          footer={foot}
        >
          {conversation}
        </MiniWindow>
      </TextStepContext.Provider>
    );
  }

  return (
    <section
      aria-labelledby="assistant-title"
      data-assistant-chat
      // On a phone it is the window (`bleed`, `routes.tsx`), so it draws no frame of its own.
      className="flex h-full min-h-0 flex-col overflow-hidden bg-surface md:rounded-lg md:border md:border-default"
    >
      <header className="flex items-center justify-between gap-4 border-b border-default px-4 py-3 md:px-5">
        {/* The page's one heading: the shell moves the focus here after a navigation. */}
        <h1
          id="assistant-title"
          data-page-title
          tabIndex={-1}
          className="flex items-center gap-2 text-heading-sm text-default"
        >
          <img src={logoUrl} alt="" className="size-5 rounded-xs" />
          Factorial Assistant
        </h1>
        <div className="flex shrink-0 items-center gap-1">{switcher}</div>
      </header>
      <div className="min-h-0 flex-1 overflow-hidden">{conversation}</div>
      <footer className="border-t border-default px-3 py-3 md:px-5">
        <div className="mx-auto w-full max-w-4xl">{foot}</div>
      </footer>
    </section>
  );
}
