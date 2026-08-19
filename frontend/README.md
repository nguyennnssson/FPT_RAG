# RAG Workspace — Presentation Layer

Enterprise AI Knowledge Assistant UI. React 19 + Vite + TypeScript, **CSS Modules only**
(no Tailwind). Conversation-first, document-aware, built to the design brief.

## Run

```bash
npm install
npm run dev
```

## Architecture

Presentation only — no business logic. Every component follows:

```
Component/
├── Component.tsx
├── Component.module.css
└── index.ts
```

```
src/
├── app/
│   ├── App.tsx                     ← composition root (the ONLY place data is wired)
│   └── layout/
│       ├── AppLayout/              two-area shell, responsive (sidebar → off-canvas on mobile)
│       └── TopHeader/              56px: workspace · search · avatar
├── features/
│   ├── conversations/components/   HistorySidebar · ConversationList (date groups) · ConversationItem
│   ├── chat/components/            ChatView · MessageList · AssistantMessage · UserMessage · PromptComposer
│   └── sources/                    SourcesProvider/useSources · SourceDrawer · SourceCardRow · DocumentCard
├── shared/
│   ├── ui/                         Button · IconButton · Avatar · Card · Badge · Drawer · Skeleton
│   │   └── markdown/               MarkdownRenderer · CodeBlock · InlineCitation
│   └── mock/data.ts                sample data — replace with your services/hooks
├── styles/                         tokens.css (design tokens) · global.css
└── types/                          domain types (Message, Source, Citation, Conversation)
```

## Wiring your existing logic

Nothing below `app/App.tsx` knows where data comes from — components take typed props.
To integrate:

1. In `App.tsx`, replace the `conversations` / `messages` mock imports with your hooks
   (e.g. `useConversations()`, `useMessages(activeConversationId)`).
2. Pass your send handler into `<ChatView onSend={...} />`.
3. Keep your `services/` and `hooks/` untouched — import them in `App.tsx` only.

## RAG conventions built in (future-ready)

- **Citations**: assistant markdown embeds `[[cite:N]]` tokens; `MarkdownRenderer` turns each
  into a clickable `InlineCitation`. `AssistantMessage` maps `N → citation.sourceId → Source`
  and opens the `SourceDrawer`. `Message.citations` defines the mapping.
- **Source cards**: `Message.sources` renders as a `SourceCardRow` **above** the answer.
- **Source drawer** opens *over* the thread (portal) and never shrinks conversation width.
- **Streaming / confidence / chunk highlight**: `Message.status`, `Source.confidence`, and
  `Source.highlight` already exist in the types and render where present.
- **Pinned chats / rename / folders**: `Conversation.pinned` reserved; `ConversationItem` is the
  single extension point for a row menu.
