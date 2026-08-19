import type { Conversation, Message } from '../../types';

/**
 * Fresh start — no hard-coded seed data. Conversations and documents are now
 * backed entirely by the real backend (chat streams from /api/query/stream;
 * documents come from /api/documents and are uploaded via /api/upload).
 *
 * These exports are kept (empty) so existing imports keep working; the chat
 * hook falls back to an empty transcript for any conversation id.
 */
export const conversations: Conversation[] = [];

export const messagesByConversation: Record<string, Message[]> = {};
