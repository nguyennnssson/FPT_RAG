import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  Conversation,
  Feedback,
  Message,
  MessageActivity,
  MessageAttachment,
} from '../../../types';
import {
  createConversation,
  getConversation,
  queryRagStream,
  sendFeedback,
  type UserContext,
} from '../../../shared/api/client';

interface UseChatOptions {
  onConversationCreated?: (conversation: Conversation) => void;
  onConversationUpdated?: (conversation: Conversation) => void;
}

/** Real chat state: transcripts load from the history API and every new turn
 * is streamed through the RAG endpoint with its durable conversation id. */
export function useChat(
  conversationId: string | null,
  user: UserContext,
  options: UseChatOptions = {},
) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const skipLoadRef = useRef<string | null>(null);
  const messagesRef = useRef<Message[]>([]);
  const optionsRef = useRef(options);
  messagesRef.current = messages;
  optionsRef.current = options;

  const cancelInflight = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  useEffect(() => {
    if (conversationId && skipLoadRef.current === conversationId) {
      skipLoadRef.current = null;
      return;
    }
    cancelInflight();
    if (!conversationId) {
      setMessages([]);
      setLoading(false);
      return;
    }
    let active = true;
    setLoading(true);
    void getConversation(conversationId, user)
      .then((result) => {
        if (active) setMessages(result.messages);
      })
      .catch(() => {
        if (active) setMessages([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [conversationId, cancelInflight, user]);

  useEffect(() => cancelInflight, [cancelInflight]);

  const send = useCallback(
    (text: string, attachments: MessageAttachment[] = []) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      const stamp = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const userMsg: Message = {
        id: `u-${stamp}`,
        role: 'user',
        content: trimmed,
        createdAt: new Date().toISOString(),
        attachments,
      };
      const assistantId = `a-${stamp}`;
      const assistantMsg: Message = {
        id: assistantId,
        role: 'assistant',
        content: '',
        createdAt: new Date().toISOString(),
        status: 'streaming',
        activity: [{
          id: 'request',
          label: 'Preparing chat request',
          detail: 'Creating the conversation and opening the response stream.',
          status: 'running',
          elapsedMs: 0,
        }],
      };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);

      const patchAssistant = (fields: Partial<Message>) =>
        setMessages((prev) =>
          prev.map((message) =>
            message.id === assistantId ? { ...message, ...fields } : message,
          ),
        );

      const upsertActivity = (activity: MessageActivity) =>
        setMessages((prev) =>
          prev.map((message) => {
            if (message.id !== assistantId) return message;
            const current = (message.activity ?? []).filter(
              (item) => item.id !== activity.id && item.id !== 'request',
            );
            if (activity.status === 'complete') {
              return { ...message, activity: current };
            }
            // Only one operation is useful while waiting. A newly-running
            // stage replaces the previous one; errors remain until dismissed
            // by the completed response.
            const withoutRunning = current.filter((item) => item.status !== 'running');
            return { ...message, activity: [...withoutRunning, activity] };
          }),
        );

      const finishRunningActivities = (status: 'complete' | 'error') =>
        setMessages((prev) =>
          prev.map((message) =>
            message.id === assistantId
              ? {
                  ...message,
                  activity: status === 'complete'
                    ? message.activity?.filter((item) => item.status !== 'running')
                    : message.activity?.map((item) =>
                        item.status === 'running' ? { ...item, status } : item,
                      ),
                }
              : message,
          ),
        );

      void (async () => {
        let targetId = conversationId;
        try {
          if (!targetId) {
            const created = await createConversation(undefined, user);
            targetId = created.id;
            // Creating a chat updates App state and re-renders this hook. Skip
            // that one load so it cannot abort or replace the optimistic stream.
            skipLoadRef.current = targetId;
            optionsRef.current.onConversationCreated?.(created);
          }

          cancelInflight();
          const controller = new AbortController();
          abortRef.current = controller;

          await queryRagStream(
            trimmed,
            {
              onDelta: (delta) =>
                setMessages((prev) =>
                  prev.map((message) =>
                    message.id === assistantId
                      ? { ...message, content: message.content + delta }
                      : message,
                  ),
                ),
              onProgress: (event) =>
                upsertActivity({
                  id: event.stage,
                  label: event.label,
                  detail: event.detail,
                  detailKind: event.detailKind,
                  status: event.status,
                  elapsedMs: event.elapsedMs,
                }),
              onFinal: (answer) => {
                finishRunningActivities('complete');
                patchAssistant({
                  id: answer.messageId ?? assistantId,
                  content: answer.content,
                  sources: answer.sources,
                  citations: answer.citations,
                  traceId: answer.traceId ?? undefined,
                  status: 'complete',
                });
                if (targetId) {
                  void getConversation(targetId, user).then(({ conversation }) =>
                    optionsRef.current.onConversationUpdated?.(conversation),
                  );
                }
              },
              onError: (error) => {
                finishRunningActivities('error');
                upsertActivity({
                  id: error.stage || 'error',
                  label: error.message,
                  detail: error.detail,
                  status: 'error',
                  elapsedMs: error.elapsedMs,
                });
                patchAssistant({
                  id: error.messageId ?? assistantId,
                  content: `Không thể hoàn tất yêu cầu.\n\n**Lỗi:** ${error.message}`,
                  status: 'error',
                });
              },
            },
            controller.signal,
            targetId,
            user,
            undefined,
            attachments.map((attachment) => attachment.docId),
          );
          if (abortRef.current === controller) abortRef.current = null;
        } catch (error) {
          finishRunningActivities('error');
          upsertActivity({
            id: 'request',
            label: 'Could not start the chat request',
            detail: error instanceof Error ? error.message : String(error),
            status: 'error',
          });
          patchAssistant({
            content: 'Không thể tạo hoặc lưu cuộc trò chuyện. Vui lòng thử lại.',
            status: 'error',
          });
        }
      })();
    },
    [conversationId, cancelInflight, user],
  );

  const setFeedback = useCallback((messageId: string, value: Feedback) => {
    const previous = messagesRef.current;
    const index = previous.findIndex((message) => message.id === messageId);
    const target = index !== -1 ? previous[index] : null;
    const isNewVote = target != null && target.feedback !== value;

    setMessages((current) =>
      current.map((message) =>
        message.id === messageId
          ? { ...message, feedback: message.feedback === value ? null : value }
          : message,
      ),
    );

    if (isNewVote && target) {
      const userTurn = previous
        .slice(0, index)
        .reverse()
        .find((message) => message.role === 'user');
      void sendFeedback({
        query: userTurn?.content ?? '',
        rating: value === 'like' ? 'up' : 'down',
        traceId: target.traceId,
        answer: target.content,
      }, user);
    }
  }, [user]);

  const regenerate = useCallback((messageId: string) => {
    const snapshot = messagesRef.current;
    if (!conversationId || snapshot.some((message) => message.status === 'streaming')) return;
    const index = snapshot.findIndex(
      (message) => message.id === messageId && message.role === 'assistant',
    );
    const original = index >= 0 ? snapshot[index] : null;
    const userTurn = index >= 0
      ? snapshot.slice(0, index).reverse().find((message) => message.role === 'user')
      : null;
    if (!original || !userTurn) return;

    const startedAt = new Date().toISOString();
    setMessages((current) => current.map((message) => (
      message.id === messageId
        ? {
            ...message,
            content: '',
            createdAt: startedAt,
            status: 'streaming',
            sources: undefined,
            citations: undefined,
            feedback: null,
            activity: [{
              id: 'request',
              label: 'Regenerating answer',
              detail: 'Re-running retrieval and generation for the same user message.',
              status: 'running',
              elapsedMs: 0,
            }],
          }
        : message
    )));

    const patchTarget = (fields: Partial<Message>) => setMessages((current) =>
      current.map((message) => (
        message.id === messageId ? { ...message, ...fields } : message
      )),
    );
    const updateActivity = (activity: MessageActivity) => setMessages((current) =>
      current.map((message) => {
        if (message.id !== messageId) return message;
        const prior = (message.activity ?? []).filter(
          (item) => item.id !== activity.id && item.id !== 'request',
        );
        if (activity.status === 'complete') return { ...message, activity: prior };
        return {
          ...message,
          activity: [...prior.filter((item) => item.status !== 'running'), activity],
        };
      }),
    );
    const restoreWithError = (label: string, detail?: string) => patchTarget({
      ...original,
      activity: [{ id: 'regeneration', label, detail, status: 'error' }],
    });

    cancelInflight();
    const controller = new AbortController();
    abortRef.current = controller;
    void queryRagStream(
      userTurn.content,
      {
        onDelta: (delta) => setMessages((current) => current.map((message) => (
          message.id === messageId
            ? { ...message, content: message.content + delta }
            : message
        ))),
        onProgress: (event) => updateActivity({
          id: event.stage,
          label: event.label,
          detail: event.detail,
          detailKind: event.detailKind,
          status: event.status,
          elapsedMs: event.elapsedMs,
        }),
        onFinal: (answer) => {
          patchTarget({
            id: answer.messageId ?? messageId,
            content: answer.content,
            sources: answer.sources,
            citations: answer.citations,
            traceId: answer.traceId ?? undefined,
            status: 'complete',
            activity: [],
          });
          void getConversation(conversationId, user).then(({ conversation }) =>
            optionsRef.current.onConversationUpdated?.(conversation),
          );
        },
        onError: (error) => restoreWithError(error.message, error.detail),
      },
      controller.signal,
      conversationId,
      user,
      messageId,
    ).catch((error) => restoreWithError(
      'Could not regenerate the answer',
      error instanceof Error ? error.message : String(error),
    )).finally(() => {
      if (abortRef.current === controller) abortRef.current = null;
    });
  }, [conversationId, cancelInflight, user]);

  return { messages, loading, send, regenerate, setFeedback };
}
