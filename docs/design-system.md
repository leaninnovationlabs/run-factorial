# Design system

**Status:** current. The working reference for building dashboard UI (`ui/`), and the model for
UI in future projects. The decision of record is `tech-stack.md` decision 50, with decisions 42
(the shell), 51 (interaction tests) and 62 (the dashboard's charts). Where this page and a
decision disagree, the decision wins and this page is wrong.

The dashboard follows the **Atlassian Design System (ADS) as a reference, not a dependency**. No
`@atlaskit` package is installed. The ADS token *values* are copied into one CSS file, Tailwind
exposes them as role-named classes, and the design primitives in `ui/src/components/ui/` take ADS
component names and props. Tests compile the mapping and count violations, so most rules below
fail CI when broken.

The public marketing site is out of scope: it has its own fonts and styles and shares nothing with
the dashboard.

---

## Vocabulary

One word for each idea, in this page, in code comments, in file names and in test names. The
four layers are ADS's own.

| Term | Means |
|---|---|
| **Design system** | All of the below. `design-system` in a path |
| **Foundations** | The tokens (colour, type, space, shape, motion) and the role classes that name them |
| **Design primitives** | The reusable building blocks, with ADS names and props: `Button`, `Panel`, `DataTable`… |
| **Patterns** | How design primitives combine to solve one problem: a confirmation, a drawer, a loading state |
| **Reference pages** | Pages to look at, never to ship: the **guide**, the **catalogue** and the **sample** (see Reference pages) |

A feature component (`components/activity/`, `components/tickets/`…) is not a design primitive. It
composes design primitives for one feature and lives in its own folder (decision 42).

---

## Where things live

| What | File |
|---|---|
| Token values | `ui/src/theme/tokens.css` |
| Tokens → Tailwind classes | `ui/src/index.css` (`@theme inline`) |
| Fonts | `ui/src/theme/fonts/`, `fonts.css` |
| Shared animation beats | `ui/src/theme/motion.css` |
| Theme choice | `ui/src/theme/ThemeProvider.tsx`, `themes.ts` |
| Design primitives | `ui/src/components/ui/`, exported from its `index.ts` |
| Status vocabulary and colour | `ui/src/status.ts` |
| Overlay stack | `ui/src/components/ui/layers.ts` |
| Dashboard chart looks | `ui/src/components/dashboard/dashboard.css` |
| Enforcement | `ui/src/theme/foundations.test.ts`, `theme.test.ts` |
| Reference pages | `ui/src/pages/` and `ui/design-system/` (see Reference pages) |

---

## Foundations

The values, and the classes that name them. `tokens.css` is the only file that holds a value:
the `@atlaskit/tokens` 16.12.0 values under their ADS names (`--ds-*`).

### Colour

**Name a role, never a colour.** A class says what the thing *is*, and the theme decides the value.

```tsx
<p className="text-subtle">…</p>                 // ✅ role
<div className="bg-surface-sunken border">…</div> // ✅ bare `border` already takes --ds-border
<p className="text-slate-500">…</p>              // ❌ does not compile: Tailwind's palette is off
<div style={{ color: "#44546f" }}>…</div>        // ❌ hex outside tokens.css fails the ratchet
```

Tailwind's own palette (`--color-*`), size scale (`--text-*`) and shadows (`--shadow-*`) are reset
to `initial`, so `bg-red-50`, `text-sm` and `shadow-md` produce no CSS at all.

The role classes, by property:

- **Text:** `text-default`, `text-subtle`, `text-subtlest`, `text-disabled`, `text-inverse`,
  `text-selected`, `text-brand`, and the status inks `text-danger | warning | success |
  information | discovery`.
- **Icons:** `text-icon`, `text-icon-subtle`, `text-icon-subtlest`, `text-icon-{status}`, …
- **Surfaces:** `bg-surface` (cards, panels), `bg-surface-sunken` (the page ground),
  `bg-surface-raised`, `bg-surface-overlay` (popups, drawers), `bg-table-head`, `bg-blanket`.
- **Interaction fills:** `bg-neutral`, `bg-neutral-subtle`, `bg-neutral-bold`, each with
  `-hovered` and `-pressed`; `bg-selected(-hovered|-pressed)`; `bg-input(-hovered|-pressed)`.
- **Status fills:** `bg-{danger|warning|success|information|discovery}` (subtle) and
  `bg-{status}-bold`, each with `-hovered`.
- **Borders and rings:** `border-default | bold | input | focused | selected | disabled |
  {status}`; `ring-*` likewise; `outline-focused`.
- **Charts:** `bg-chart-{neutral|information|warning|danger|success|discovery}`.

The full list is the `@theme inline` block in `ui/src/index.css`. To use a token that has no class
yet, copy it into `tokens.css` under its ADS name (all theme blocks) and add the mapping there.
Hand-written CSS reads the token directly: `var(--ds-surface)`, `var(--ds-space-200)`.

#### Colour rules that look wrong but are deliberate

The reasons are in decision 50.

- **The primary button is neutral-bold (near-black), not ADS blue.**
- **A link is not blue.** It takes the colour of the text around it and is marked by weight and an
  underline.
- **A selection carries an indicator** (a bar, a check, a border), never a background alone.
- **Hover, pressed and selected come from the finesse set together.**
- **Dark mode changes colour and nothing else.** The one exception is the nav mark's blend in
  `theme/dark.css`.

#### Status colour

A ticket's or run's state is drawn through one path, so a header, a tag and a row never disagree:

```
TicketCondition / StepRunStatus ──CONDITION_TONES──▶ ConditionTone ──TONE_ROLE──▶ ADS role
```

Both tables, and why each state takes its colour, are in `ui/src/status.ts`. Use
`<StatusLozenge status={…} />` or `statusFacts()`. Never pick a colour for a state by hand.

### Typography

One scale, from ADS; the sizes, line heights and weights are in `ui/src/index.css`. Each heading
and metric size brings its line height and weight, so you do not add `font-bold` beside it.

| Class | For |
|---|---|
| `text-heading-xl` | rare: a landing heading |
| `text-heading-lg` | page title (`PageHeader`) |
| `text-heading-md` | a section of a long page |
| `text-heading-sm` | panel title |
| `text-heading-xs` | sub-section |
| `text-heading-xxs` | small group label |
| `text-body-lg` | long-form reading |
| `text-body` | **default body** |
| `text-body-sm` | metadata, captions. **12px is the floor** |
| `text-metric-lg | md | sm` | numbers in `Metric`, `StatTile` |
| `text-code` | inline code inside running text |

Sizes and colours never share a name: `text-body-sm` is a size, `text-subtle` a colour.
Weights: `font-normal`, `font-medium`, `font-semibold`, `font-bold` (ADS values).
Fonts: `font-sans` (Inter), `font-mono` (JetBrains Mono).

- No arbitrary sizes (`text-[13px]`), and no stylesheet `font-size` under 12px. Both are ratcheted.
- **Sentence case everywhere.** "Save changes", not "Save Changes" or "SAVE". All-caps copy
  fails the ratchet unless the word is a known acronym (`PR`, `CI`, `API`, …; see `ACRONYMS` in
  `foundations.test.ts`).

### Space, shape, elevation, layers

**Spacing** uses Tailwind's scale, which is the ADS scale; the step-by-step mapping is in the
header of `tokens.css`. Steps `2.5`, `3.5` and `9` are not on it. Common rhythm: `gap-2` inside a
control group, `p-4` / `space-y-4` inside a panel, `gap-6` between panels.

**Radius:** `rounded-xs` to `rounded-xl`, and `rounded-full`.

**Elevation:** `shadow-raised`, `shadow-overlay`, `shadow-overflow`, for things that move or float
only. A `Panel` is part of the page, so it has an edge and no shadow.

**Layers** (`z-*`): `card`, `navigation`, `dialog`, `layer`, `blanket`, `modal`, `popup`, `flag`,
`spotlight`, `tooltip`, lowest to highest (values in `index.css`). `popup` is ours, so a menu
opened from a drawer stacks above it.

**Page widths:** data pages take the window; `max-w-page-default` for runtime pages,
`max-w-page-narrow` for create forms.

**Opacity:** `opacity-disabled`, `opacity-loading`.

---

## Design primitives

Build from `components/ui` first, imported from the folder's index. To find the one you need,
open the catalogue: it groups the design primitives by what they are for (actions, labels,
messages, forms, navigation, loading and empty, code and figures, overlays, tables), and its
source shows each one in use. The complete list is `ui/src/components/ui/index.ts`.

### Props follow ADS

Variants are props, not classes: `appearance`, `spacing`, `isSelected`, `isLoading`,
`isDisabled`, `shouldFitContainer`.

```tsx
<Button appearance="primary">Approve</Button>
<Button appearance="subtle" spacing="compact" iconBefore="refresh">Retry</Button>
<Button appearance="danger" isLoading={pending}>Delete project</Button>
<Lozenge appearance="warning">Waiting</Lozenge>
```

Each design primitive's props are its type; the catalogue draws every appearance.

### Rules

- **No raw `<button>` outside `components/ui/`.** The two picture components
  (`factory-board/Board|Band`, `flow/FlowGraph`) are the only exceptions. Ratcheted.
- **No padding or text-size classes on `<Button>`**: they lose to its own on Tailwind's class
  order. Use `spacing` or a new appearance. Ratcheted.
- **No `title=` attribute** (native tooltips show on mouse hover only). Put the text on the page
  or in a `Tooltip` on a focusable trigger. Ratcheted.
- **A new design primitive** goes in `components/ui/`, takes an ADS name and props, is exported from
  `index.ts`, gets a section in the catalogue, and is used in the sample once a page needs it.
- `components/ui` never imports from a feature folder (for example `components/dashboard`).

---

## Patterns

The reasons are in decision 50.

- **Confirmation weighs what it guards.** Undoable action → `ConfirmPopup` anchored to its
  button. Irreversible action → `ConfirmDialog` (modal). Discarding a local draft → do it at once
  and offer Undo in a flag.
- **Reading surfaces and edit forms open in a `Drawer`**: modal, slides in from the right, dims
  the page, never shifts the layout.
- **Overlays use the layer stack** (`layers.ts`). Escape closes only the top layer. A modal layer
  makes everything under it `inert`, which contains focus without a Tab trap. Do not build a
  second overlay mechanism, and do not add a headless component library (deferred in
  `tech-stack.md` §5).
- **Action feedback** goes through `useActionFeedback` / `showFlag`. A slow action says so after
  `SLOW_AFTER_MS`.
- **Data loading** wraps a query in `QueryBoundary`, with a skeleton shaped like the content, not
  a page-wide spinner.

---

## Phone width

Below `md` (48rem, 768px), the dashboard is for two tasks: tickets and the assistant. Other
pages must not scroll sideways, but they get no phone layout of their own. The reasons are in
decision 50.

- **The tab bar** (`shell/TabBar.tsx`) is at the foot of the window: Tickets, Assistant and Menu.
  Tickets shows only in a project, and goes to the tickets of that project. Menu opens the side
  navigation over the page. The top bar has no menu button on a phone.
- **A layout change in script** uses `useIsPhone()` (`components/ui/media.ts`). Use it only when
  the phone draws a different thing, for example a list in place of a board. When only the
  layout changes, use a `max-md:` or `md:` class.
- **Overview** shows the issue list without the board, the instruments and Board settings. A row
  is 44px high (`ROW_PX_TOUCH`), so a thumb can press it.
- **A table** that gives `DataTable` a `stack` shows each row as a stacked item: the title, one
  fact at its right, and the named facts under it. A select above the items replaces the sort in
  the column heads. A table without `stack` scrolls in its own region.
- **A ticket**: the process graph does not stay at the top, the rail of runs is hidden, and the
  activity panel is nearly the height of the window. The Output drawer shows one section at a
  time.
- **A drawer** fills the window. The assistant's page (`bleed` in `routes.tsx`) goes to the edges
  of the window.
- **A field** is 16px on a phone, because iOS Safari zooms into a smaller field when it gets the
  focus.
- **Insets:** the tab bar and a drawer's footer keep clear of the home indicator
  (`env(safe-area-inset-bottom)`; `viewport-fit=cover` in `index.html`). The flags and the
  assistant's Show-me bar stay above the tab bar (`--tab-bar-clearance` in `index.css`).

---

## Accessibility

- **Focus:** one global indicator, 2px `--ds-border-focused`, offset 2px, keyboard focus only
  (`index.css`). Do not restyle or remove it. The exception is a field that takes text: a
  browser counts a click into it as keyboard focus, so it shows no ring. Its focus is a 2px
  `border-selected` edge, drawn inside the field. An invalid field's edge is `border-danger`.
  Forced colours keep the ring. A control clipped by its container adds
  `focus-inset`. `tabindex="-1"` targets (a page title, `<main>`) show no ring.
- **Contrast:** text 4.5:1 and a control's edge 3:1 in both themes, checked by `foundations.test.ts`.
  `prefers-contrast: more` swaps in the ADS increased-contrast values.
- **Motion:** a looping animation beats three times and stops (WCAG 2.2.2), and stops under
  reduced motion (`prefersReducedMotion()` in `components/ui/motion.ts`). Reuse the beats in
  `motion.css`. Default transitions are `transition-colors` (150ms, eased out).
- **Shortcuts:** never a single character (WCAG 2.1.4). Search is ⌘K / Ctrl+K. A focus trap needs a
  way out: the terminal releases on Ctrl+] (WCAG 2.1.2).
- **Status is never colour alone:** a lozenge carries a word, a chart carries a legend and a table
  twin ("View data").
- **Tests:** a test that needs focus, `inert` or keys opts into
  `// @vitest-environment happy-dom` (decision 51); everything else renders to string under node.

---

## Charts

- **Assistant charts** (`components/charts/`): hand-drawn SVG on `--ds-chart-*` in a fixed order.
- **Dashboard figures** (`components/dashboard/`): CSS bars and stacked bars. A mark paints
  `var(--look)`, and a look is a `--dash-*` property in `dashboard.css`, a tint of a chart token.
  No colour value leaves `tokens.css`, and no `.tsx` names one.
- No chart library (recharts, d3, visx) until a figure needs a continuous axis or more than one
  time scale (decision 62).
- Every chart shows its numbers as text too: a tooltip, a legend and a table twin.

---

## Enforcement

`ui/src/theme/foundations.test.ts` runs with the UI tests (`make check` and CI). It compiles
`index.css` and checks the tokens (both themes, contrast), and its ratchet counts every breach of
the rules on this page across `ui/src`. What each counter catches is written beside it in
`BASELINE`; every baseline is 0, and a count that moves either way fails.

---

## Reference pages

Three pages show the design system instead of describing it. Start with the **guide**: it renders
this document, draws the colours live, and links to the other two. Start them with one
command; they need no API, database or account, because in development builds their routes
render ahead of the session check (`ui/src/App.tsx`):

```bash
make design-system          # http://localhost:5180/factorial/dev/guide, /dev/catalogue, /dev/sample
make design-system-update   # write ui/design-system/*.html again after a change
```

Without a server, open `ui/design-system/guide.html`.

| | Guide | Catalogue | Sample |
|---|---|---|---|
| Shows | This document, with the colours drawn live | Every design primitive in every state | Design primitives composed into one realistic page |
| Use it to | Read the design system end to end | Pick a design primitive and see its props | See how a page is laid out: shell, header, panels, table, form |
| Component | `ui/src/pages/GuidePage.tsx` | `ui/src/pages/CataloguePage.tsx` | `ui/src/pages/SamplePage.tsx` |
| Interactive (`make design-system`) | `/dev/guide` | `/dev/catalogue` | `/dev/sample` |
| Static file | `ui/design-system/guide.html` | `ui/design-system/catalogue.html` | `ui/design-system/sample.html` |
| Test | `GuidePage.test.tsx` | `CataloguePage.test.tsx` | `SamplePage.test.tsx` |

A new reference page follows the same pattern: `<Name>Page.tsx`, `/dev/<name>` (in
`REFERENCE_ROUTES` in `ui/src/App.tsx` and `ORGLESS` in `ui/src/org.tsx`), an entry in
`REFERENCE_PAGES` in `ui/src/pages/ReferenceNav.tsx`, `ui/design-system/<name>.html`,
`<Name>Page.test.tsx`, and a line in `make design-system-update`.

- **The static files are generated, not hand-written.** Each test renders its page with
  `pages/referenceHtml.tsx`: static HTML, only the Tailwind classes the page uses, and the CSS,
  tokens and fonts inlined. A file loads nothing from disk or the network, so it opens with a
  double-click and can be copied into another repository as-is.
- **They cannot drift.** Each test compares its output with the checked-in file. A change to a
  design primitive, a token, a reference page or this document (the guide renders it) fails the
  test until `make design-system-update` writes the file again. Look at the diff before
  committing it. CI never writes snapshots, so a stale or missing file fails there.
- **They are the same on every machine.** `pages/referenceEnvironment.ts` pins the clock
  (2026-09-14 12:00 UTC), the time zone (UTC) and the locale (`en-US`) before anything formats a
  date, so a relative time or a clock time renders the same everywhere.
- **Every header has the same controls** (`pages/ReferenceNav.tsx`): links to the other two
  pages and the theme switch (light, dark, match system). In the app a link is the route
  `/dev/<name>` and the switch sets the dashboard's own theme; in a static file the renderer
  rewrites a link to `<name>.html`, the file beside it, and a small script does the switching.
- **They are static as files.** Tabs and forms do not act, and a link into the app stays on the
  page. Overlays (drawers, dialogs, popups, tooltips) portal to the body, which a server render
  cannot draw, so the catalogue file shows only their triggers. For the interactive pages run
  `make design-system`. Increased contrast follows the OS setting only.
- **To show something new**, add it to the page with fixed data (no query, no open portal) and
  regenerate.

### Using them in another project

The reference pages are the visual contract. A project that wants the same look takes three
things, and keeps the names in this page so the code and the docs stay comparable:

1. The foundations: `ui/src/theme/tokens.css` (the values) and, from `ui/src/index.css`, the
   `@theme inline` block and the base and focus rules (the role classes). Copy them as-is; change
   values only in `tokens.css`.
2. This page, as the rules.
3. `ui/design-system/guide.html`, `catalogue.html` and `sample.html`, to read and to compare
   against side by side.

The design primitives are React. A project on another stack rebuilds the ones it needs under the
same names and props, and checks them against the catalogue. Sharing them as a package is a
separate decision, not made yet (`tech-stack.md` §5 rules out new libraries without one).

---

## Checklist for a UI change

1. Start from the catalogue (Design primitives).
2. Follow Foundations, Patterns and Accessibility.
3. Look at the change in light, dark and increased contrast, and at phone width when it touches
   tickets or the assistant (Phone width).
4. Run `make check`. A failing reference page test is covered under Reference pages.
