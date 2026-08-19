export type ISODate = string;

export type MessageRole = 'user' | 'assistant';
export type MessageStatus = 'streaming' | 'complete' | 'error';
export type Feedback = 'like' | 'dislike';

export interface Source {
 id: string;
 docId?: string;
 chunkId?: string;
 name: string;
 page?: string;
 /** Exact raw chunk cited by the answer. */
 text?: string;
 snippet?: string;
 highlight?: string;
 confidence?: number; // 0..1
 sourceUri?: string;
 sectionPath?: string[];
}

export interface Citation {
 id: string;
 index: number;
 sourceId: string;
}

export interface MessageAttachment {
 docId: string;
 name: string;
 kind: 'image' | 'file';
 previewUrl?: string;
 mimeType?: string;
}

export type MessageActivityStatus = 'running' | 'complete' | 'error';

export interface MessageActivity {
 id: string;
 label: string;
 detail?: string;
 detailKind?: 'text' | 'command';
 status: MessageActivityStatus;
 elapsedMs?: number;
}

export interface Message {
 id: string;
 role: MessageRole;
 content: string; // markdown, may contain [[cite:N]] tokens
 createdAt: ISODate;
 status?: MessageStatus;
 sources?: Source[];
 citations?: Citation[];
 attachments?: MessageAttachment[];
 feedback?: Feedback | null;
 /** Backend trace id for this answer — sent with /feedback. */
 traceId?: string;
 /** Safe operational events shown while retrieval/generation is running. */
 activity?: MessageActivity[];
}

export interface Conversation {
 id: string;
 title: string;
 updatedAt: ISODate;
 pinned?: boolean;
 deletedAt?: ISODate | null;
 purgeAfter?: ISODate | null;
}

export type MemoryKind = 'preference' | 'fact' | 'instruction';

export interface UserMemory {
 id: string;
 kind: MemoryKind;
 content: string;
 isExplicit: boolean;
 confidence: number;
 sourceConversationId?: string | null;
 createdAt: ISODate;
 updatedAt: ISODate;
}

export type DocumentStatus =
 | 'processing'
 | 'active'
 | 'failed';

export interface UploadedDocument {
 id: string;
 name: string;
 folder: string;
 fileType: string;
 size: number; // bytes (0 when unknown, e.g. server-indexed docs)
 meta?: string; // display label shown instead of size when set (e.g. "12 chunks")
 status?: DocumentStatus; // upload lifecycle; absent for already-indexed server docs
 chunks?: number; // indexed chunk count (from the backend)
 error?: string; // failure detail when status === 'error'
 indexedAt?: string; // ISO timestamp from the backend
}

export interface UploadResult {
 fileName: string;
 docId: string;
 status: 'active' | 'failed';
 chunks?: number;
 error?: string;
}
