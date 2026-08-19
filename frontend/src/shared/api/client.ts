import type { Citation, Conversation, Message, MemoryKind, Source, UserMemory } from '../../types';
import { projectCitations } from './citations';

/**
 * RAG backend client. Talks to the FastAPI service (see backend/api/main.py).
 * In dev, requests go to `/api/*` and Vite proxies them to the backend
 * (vite.config.ts), so there are no CORS concerns. Override the base with
 * VITE_API_BASE_URL if the backend is hosted elsewhere.
 */
const BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api').replace(/\/$/, '');

export function documentFileUrl(docId: string): string {
  return `${BASE}/documents/${encodeURIComponent(docId)}/file`;
}

/** Demo identity sent as the body-fallback user (backend/api/auth.py). A real
 *  deployment injects identity via SSO/proxy headers instead. */
export interface UserContext {
  tenant_id: string;
  user_id: string;
  principals: string[];
  classification?: 'public' | 'internal' | 'confidential' | 'restricted';
}

export const DEMO_USER: UserContext = {
  tenant_id: 'demo',
  user_id: 'web-user',
  principals: ['group:all-employees'],
  classification: 'internal',
};

function identityHeaders(user: UserContext): Record<string, string> {
  return {
    'X-Tenant-Id': user.tenant_id,
    'X-User-Id': user.user_id,
    'X-Principals': user.principals.join(','),
    'X-Classification': user.classification ?? 'internal',
  };
}

// --- Wire types (mirror api/schemas.py) ------------------------------------ //

interface WireSource {
  label: string;
  chunk_id: string;
  doc_id: string;
  title: string;
  source_uri: string;
  text: string;
  section_path: string[];
  page_number: number | null;
}

interface WireAnswer {
  answer: string;
  abstained: boolean;
  abstention_reason: string | null;
  language: string;
  model: string | null;
  trace_id: string | null;
  message_id?: string | null;
  sources: WireSource[];
}

/** UI-shaped answer: markdown content with [[cite:N]] tokens, plus the source
 *  and citation lists the AssistantMessage component expects. */
export interface UiAnswer {
  content: string;
  sources: Source[];
  citations: Citation[];
  abstained: boolean;
  model: string | null;
  traceId: string | null;
  messageId: string | null;
}

// --- Mapping: backend [S#] citations -> UI [[cite:N]] ---------------------- //

/** Turn a backend WireSource into the UI Source, carrying provenance into the
 *  drawer snippet (the query API doesn't return chunk text). */
function toUiSource(s: WireSource, displayIndex?: number): Source {
  const section = s.section_path?.length ? ` · ${s.section_path.join(' › ')}` : '';
  const text = s.text?.trim();
  return {
    id: displayIndex == null ? s.label : `S${displayIndex}`,
    docId: s.doc_id,
    chunkId: s.chunk_id,
    name: s.title || s.doc_id,
    page: s.page_number != null ? `Trang ${s.page_number}` : undefined,
    text: text || undefined,
    snippet: text || `Nguồn: ${s.doc_id} (${s.source_uri})${section}`,
    sourceUri: s.source_uri,
    sectionPath: s.section_path,
  };
}

function mapAnswer(w: WireAnswer): UiAnswer {
  const projected = projectCitations(w.answer, w.sources);
  const sources = projected.sources.map(({ source, displayIndex }) => (
    toUiSource(source, displayIndex)
  ));
  const citations: Citation[] = sources.map((source, index) => ({
    id: `cite-${index + 1}`,
    index: index + 1,
    sourceId: source.id,
  }));
  return {
    content: projected.content,
    sources,
    citations,
    abstained: w.abstained,
    model: w.model,
    traceId: w.trace_id,
    messageId: w.message_id ?? null,
  };
}

// --- Public API ------------------------------------------------------------ //

/** Blocking query — returns a fully-formed UI answer. */
export async function queryRag(
  question: string,
  signal?: AbortSignal,
  user: UserContext = DEMO_USER,
  attachmentDocIds: string[] = [],
): Promise<UiAnswer> {
  const res = await fetch(`${BASE}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: question, attachment_doc_ids: attachmentDocIds, user }),
    signal,
  });
  if (!res.ok) throw new Error(`Query failed (${res.status})`);
  return mapAnswer((await res.json()) as WireAnswer);
}

export interface StreamHandlers {
  onDelta: (text: string) => void;
  onFinal: (answer: UiAnswer) => void;
  onProgress?: (event: StreamProgress) => void;
  onError?: (err: StreamFailure) => void;
}

export interface StreamProgress {
  stage: string;
  status: 'running' | 'complete' | 'error';
  label: string;
  detail?: string;
  detailKind?: 'text' | 'command';
  elapsedMs?: number;
}

export interface StreamFailure {
  stage: string;
  message: string;
  detail?: string;
  retryable: boolean;
  elapsedMs?: number;
  messageId?: string;
}

/** Streaming query over SSE. Calls onDelta as tokens arrive, then onFinal with
 *  the assembled answer (sources + citations). */
export async function queryRagStream(
  question: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
  conversationId?: string,
  user: UserContext = DEMO_USER,
  regenerateMessageId?: string,
  attachmentDocIds: string[] = [],
): Promise<void> {
  try {
    const res = await fetch(`${BASE}/query/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: question,
        conversation_id: conversationId,
        regenerate_message_id: regenerateMessageId,
        attachment_doc_ids: attachmentDocIds,
        user,
      }),
      signal,
    });
    if (!res.ok || !res.body) throw new Error(`Stream failed (${res.status})`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let terminalSeen = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let sep: number;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const dataLine = frame
          .split('\n')
          .find((l) => l.startsWith('data:'));
        if (!dataLine) continue;
        const json = dataLine.slice(5).trim();
        if (!json || json === '{}') continue;
        let event: {
          type?: string;
          text?: string;
          stage?: string;
          status?: 'running' | 'complete' | 'error';
          label?: string;
          detail?: string;
          detail_type?: 'text' | 'command';
          message?: string;
          retryable?: boolean;
          elapsed_ms?: number;
          message_id?: string;
        } & Partial<WireAnswer>;
        try {
          event = JSON.parse(json);
        } catch {
          continue;
        }
        if (event.type === 'delta' && event.text) {
          handlers.onDelta(event.text);
        } else if (
          event.type === 'progress'
          && event.stage
          && event.status
          && event.label
        ) {
          handlers.onProgress?.({
            stage: event.stage,
            status: event.status,
            label: event.label,
            detail: event.detail,
            detailKind: event.detail_type,
            elapsedMs: event.elapsed_ms,
          });
        } else if (event.type === 'error') {
          terminalSeen = true;
          handlers.onError?.({
            stage: event.stage ?? 'request',
            message: event.message ?? 'The chat request failed.',
            detail: event.detail,
            retryable: event.retryable ?? true,
            elapsedMs: event.elapsed_ms,
            messageId: event.message_id,
          });
        } else if (event.type === 'final') {
          terminalSeen = true;
          handlers.onFinal(mapAnswer(event as WireAnswer));
        }
      }
    }
    if (!terminalSeen) {
      throw new Error('Stream ended without a final answer or error event.');
    }
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') return;
    handlers.onError?.({
      stage: 'connection',
      message: 'Could not maintain the connection to the RAG service.',
      detail: err instanceof Error ? err.message : String(err),
      retryable: true,
    });
  }
}

// --- Conversation history ------------------------------------------------- //

interface WireConversation {
  id: string;
  title: string;
  pinned: boolean;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  purge_after: string | null;
}

interface WireHistoryMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  status: 'streaming' | 'complete' | 'error';
  created_at: string;
  trace_id: string | null;
  feedback: 'up' | 'down' | null;
  sources: WireSource[];
}

interface WireConversationDetail extends WireConversation {
  messages: WireHistoryMessage[];
}

function toConversation(item: WireConversation): Conversation {
  return {
    id: item.id,
    title: item.title,
    pinned: item.pinned,
    updatedAt: item.updated_at,
    deletedAt: item.deleted_at,
    purgeAfter: item.purge_after,
  };
}

function toMessage(item: WireHistoryMessage): Message {
  const storedUser = item.role === 'user'
    ? mapStoredUserMessage(item.content)
    : { content: item.content, attachments: [] };
  const projected = item.role === 'assistant'
    ? projectCitations(item.content, item.sources)
    : { content: item.content, sources: [] };
  const sources = projected.sources.map(({ source, displayIndex }) => (
    toUiSource(source, displayIndex)
  ));
  const citations: Citation[] = sources.map((source, index) => ({
    id: `cite-${index + 1}`,
    index: index + 1,
    sourceId: source.id,
  }));
  return {
    id: item.id,
    role: item.role,
    content: item.role === 'user' ? storedUser.content : projected.content,
    createdAt: item.created_at,
    status: item.status,
    traceId: item.trace_id ?? undefined,
    feedback: item.feedback === 'up' ? 'like' : item.feedback === 'down' ? 'dislike' : null,
    sources: sources.length ? sources : undefined,
    citations: citations.length ? citations : undefined,
    attachments: storedUser.attachments.length ? storedUser.attachments : undefined,
  };
}

function mapStoredUserMessage(content: string): {
  content: string;
  attachments: NonNullable<Message['attachments']>;
} {
  const attachments: NonNullable<Message['attachments']> = [];
  const seen = new Set<string>();
  const addAttachment = (rawDocId: string) => {
    // Old announcement sentences ended with a period ("image.png."). It is
    // punctuation, not part of the uploaded document id.
    const docId = rawDocId.replace(/\.+$/, '');
    if (!docId || seen.has(docId)) return;
    seen.add(docId);
    attachments.push({
      docId,
      name: docId,
      kind: /(?:^|[._-])(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(docId)
        || docId.toLocaleLowerCase().startsWith('image')
        ? 'image'
        : 'file',
      previewUrl: documentFileUrl(docId),
    });
  };
  let clean = content.replace(
    /\s*\[\[attachment:([A-Za-z0-9._-]+)\]\]/g,
    (_marker, docId: string) => {
      addAttachment(docId);
      return '';
    },
  );
  // Backward compatibility for messages created before attachments were
  // structured. Convert the old synthetic announcement into real attachment
  // media so reopening the conversation shown in the bug report fixes it too.
  clean = clean.replace(
    /\s*Tệp đính kèm đã được tải lên:\s*([A-Za-z0-9._-]+(?:\s*,\s*[A-Za-z0-9._-]+)*)\.?(?=\s*$)/i,
    (_announcement, names: string) => {
      names.split(',').map((name) => name.trim()).filter(Boolean).forEach(addAttachment);
      return '';
    },
  );
  return { content: clean.trim(), attachments };
}

export async function listConversations(user: UserContext = DEMO_USER): Promise<Conversation[]> {
  const res = await fetch(`${BASE}/conversations`, { headers: identityHeaders(user) });
  if (!res.ok) throw new Error(`Conversation list failed (${res.status})`);
  const body = (await res.json()) as { conversations: WireConversation[] };
  return body.conversations.map(toConversation);
}

export async function listDeletedConversations(
  user: UserContext = DEMO_USER,
): Promise<Conversation[]> {
  const res = await fetch(`${BASE}/conversations?deleted=true`, {
    headers: identityHeaders(user),
  });
  if (!res.ok) throw new Error(`Deleted conversation list failed (${res.status})`);
  const body = (await res.json()) as { conversations: WireConversation[] };
  return body.conversations.map(toConversation);
}

export async function createConversation(
  title?: string,
  user: UserContext = DEMO_USER,
): Promise<Conversation> {
  const res = await fetch(`${BASE}/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, user }),
  });
  if (!res.ok) throw new Error(`Conversation creation failed (${res.status})`);
  return toConversation((await res.json()) as WireConversation);
}

export async function getConversation(
  conversationId: string,
  user: UserContext = DEMO_USER,
): Promise<{ conversation: Conversation; messages: Message[] }> {
  const res = await fetch(`${BASE}/conversations/${encodeURIComponent(conversationId)}`, {
    headers: identityHeaders(user),
  });
  if (!res.ok) throw new Error(`Conversation load failed (${res.status})`);
  const body = (await res.json()) as WireConversationDetail;
  return { conversation: toConversation(body), messages: body.messages.map(toMessage) };
}

export async function summarizeConversation(
  conversationId: string,
  user: UserContext = DEMO_USER,
): Promise<{ summary: string; messageCount: number }> {
  const res = await fetch(
    `${BASE}/conversations/${encodeURIComponent(conversationId)}/summary`,
    { method: 'POST', headers: identityHeaders(user) },
  );
  if (!res.ok) throw new Error(`Conversation summary failed (${res.status})`);
  const body = (await res.json()) as { summary: string; message_count: number };
  return { summary: body.summary, messageCount: body.message_count };
}

export async function updateConversation(
  conversationId: string,
  changes: { title?: string; pinned?: boolean },
  user: UserContext = DEMO_USER,
): Promise<Conversation> {
  const res = await fetch(`${BASE}/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...changes, user }),
  });
  if (!res.ok) throw new Error(`Conversation update failed (${res.status})`);
  return toConversation((await res.json()) as WireConversation);
}

export async function deleteConversation(
  conversationId: string,
  user: UserContext = DEMO_USER,
): Promise<void> {
  const res = await fetch(`${BASE}/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'DELETE',
    headers: identityHeaders(user),
  });
  if (!res.ok) throw new Error(`Conversation deletion failed (${res.status})`);
}

export async function restoreConversation(
  conversationId: string,
  user: UserContext = DEMO_USER,
): Promise<Conversation> {
  const res = await fetch(
    `${BASE}/conversations/${encodeURIComponent(conversationId)}/restore`,
    { method: 'POST', headers: identityHeaders(user) },
  );
  if (!res.ok) throw new Error(`Conversation restore failed (${res.status})`);
  return toConversation((await res.json()) as WireConversation);
}

// --- Long-term user memories ---------------------------------------------- //

interface WireMemory {
  id: string;
  kind: MemoryKind;
  content: string;
  is_explicit: boolean;
  confidence: number;
  source_conversation_id: string | null;
  created_at: string;
  updated_at: string;
}

function toMemory(item: WireMemory): UserMemory {
  return {
    id: item.id,
    kind: item.kind,
    content: item.content,
    isExplicit: item.is_explicit,
    confidence: item.confidence,
    sourceConversationId: item.source_conversation_id,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
  };
}

export async function listMemories(user: UserContext = DEMO_USER): Promise<UserMemory[]> {
  const res = await fetch(`${BASE}/memories`, { headers: identityHeaders(user) });
  if (!res.ok) throw new Error(`Memory list failed (${res.status})`);
  const body = (await res.json()) as { memories: WireMemory[] };
  return body.memories.map(toMemory);
}

export async function createMemory(
  content: string,
  kind: MemoryKind,
  user: UserContext = DEMO_USER,
): Promise<UserMemory> {
  const res = await fetch(`${BASE}/memories`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, kind, user }),
  });
  if (!res.ok) throw new Error(`Memory creation failed (${res.status})`);
  return toMemory((await res.json()) as WireMemory);
}

export async function deleteMemory(
  memoryId: string,
  user: UserContext = DEMO_USER,
): Promise<void> {
  const res = await fetch(`${BASE}/memories/${encodeURIComponent(memoryId)}`, {
    method: 'DELETE',
    headers: identityHeaders(user),
  });
  if (!res.ok) throw new Error(`Memory deletion failed (${res.status})`);
}

// --- Documents ------------------------------------------------------------- //

export interface IngestResult {
  doc_id: string;
  status: string;
  chunks_indexed: number;
  chunks_quarantined: number;
  message: string | null;
}

/** Ingest already-extracted text as a document (admin/document-management). */
export async function ingestDocument(params: {
  docId: string;
  text: string;
  title?: string;
  sourceUri?: string;
}, user: UserContext = DEMO_USER): Promise<IngestResult> {
  const res = await fetch(`${BASE}/ingest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      doc_id: params.docId,
      text: params.text,
      title: params.title,
      source_uri: params.sourceUri,
      content_type: 'text/plain',
      acl_principals: ['*'],
      sensitivity: 'internal',
      user,
    }),
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = JSON.stringify((await res.json()).detail ?? detail);
    } catch {
      /* ignore */
    }
    throw new Error(`Ingest failed: ${detail}`);
  }
  return (await res.json()) as IngestResult;
}

/** Upload a real file (binary-capable) — the server extracts text and runs the
 *  full ingestion pipeline. This is the multipart counterpart to ingestDocument
 *  (which only takes already-extracted text) and is what the Documents page uses
 *  so .pdf/.docx actually get indexed. */
export async function uploadDocument(
  file: File,
  opts: { title?: string; sensitivity?: string; acl?: string } = {},
  user: UserContext = DEMO_USER,
): Promise<IngestResult> {
  const form = new FormData();
  form.append('file', file);
  if (opts.title) form.append('title', opts.title);
  form.append('sensitivity', opts.sensitivity ?? 'internal');
  form.append('acl', opts.acl ?? '*');
  const res = await fetch(`${BASE}/upload`, {
    method: 'POST',
    headers: identityHeaders(user),
    body: form,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = JSON.stringify((await res.json()).detail ?? detail);
    } catch {
      /* ignore */
    }
    throw new Error(`Upload failed: ${detail}`);
  }
  return (await res.json()) as IngestResult;
}

export interface SearchPassage {
  chunk_id: string;
  doc_id: string;
  title: string;
  source_uri: string;
  section_path: string[];
  page_number: number | null;
  text: string;
  score: number;
}

/** Retrieval-only search: ranked passages, no answer generation. Powers the
 *  "search inside documents" mode on the Documents page. */
export async function searchDocuments(
  query: string,
  topK = 10,
  user: UserContext = DEMO_USER,
): Promise<SearchPassage[]> {
  const res = await fetch(`${BASE}/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, top_k: topK, user }),
  });
  if (!res.ok) throw new Error(`Search failed (${res.status})`);
  return ((await res.json()).results ?? []) as SearchPassage[];
}

export interface BackendDocument {
  doc_id: string;
  title: string;
  source_uri: string;
  folder?: string;
  file_type?: string;
  status?: 'active';
  chunks: number;
  indexed_at: string;
}

export interface DocumentPreview {
  doc_id: string;
  title: string;
  folder: string;
  file_type: string;
  indexed_at: string;
  content: string;
  truncated: boolean;
  has_original: boolean;
  content_type: string;
  file_name: string;
}

/** List the documents currently indexed for the demo tenant. */
export async function listDocuments(user: UserContext = DEMO_USER): Promise<BackendDocument[]> {
  const res = await fetch(`${BASE}/documents`, {
    headers: identityHeaders(user),
  });
  if (!res.ok) throw new Error(`Document list failed (${res.status})`);
  return ((await res.json()).documents ?? []) as BackendDocument[];
}

export async function getDocumentPreview(
  docId: string,
  user: UserContext = DEMO_USER,
): Promise<DocumentPreview> {
  const res = await fetch(`${BASE}/documents/${encodeURIComponent(docId)}/preview`, {
    headers: identityHeaders(user),
  });
  if (!res.ok) throw new Error(`Document preview failed (${res.status})`);
  return (await res.json()) as DocumentPreview;
}

export async function getDocumentFile(
  docId: string,
  user: UserContext = DEMO_USER,
): Promise<Blob> {
  const res = await fetch(`${BASE}/documents/${encodeURIComponent(docId)}/file`, {
    headers: identityHeaders(user),
  });
  if (!res.ok) throw new Error(`Original document failed (${res.status})`);
  return res.blob();
}

export async function sendFeedback(params: {
  query: string;
  rating: 'up' | 'down';
  traceId?: string;
  answer?: string;
}, user: UserContext = DEMO_USER): Promise<void> {
  try {
    await fetch(`${BASE}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: params.query,
        rating: params.rating,
        trace_id: params.traceId,
        answer: params.answer,
        user,
      }),
    });
  } catch {
    /* feedback is best-effort */
  }
}
