import { useMemo } from 'react';
import { FileText, LoaderCircle, MessageSquareText, RefreshCw, Sparkles } from 'lucide-react';
import type { Message, Source } from '../../../../types';
import { MarkdownRenderer } from '../../../../shared/ui/markdown/MarkdownRenderer';
import { useSources } from '../../../sources/context';
import styles from './ChatOverview.module.css';

interface ChatOverviewProps {
  title: string;
  messages: Message[];
  summary?: string;
  summaryLoading?: boolean;
  summaryError?: string;
  onSummarize?: () => void;
  onClose?: () => void;
}

interface UsedDocument {
  key: string;
  name: string;
  pages: string[];
  references: number;
  source: Source;
}

export function ChatOverview({
  title,
  messages,
  summary,
  summaryLoading = false,
  summaryError,
  onSummarize,
  onClose,
}: ChatOverviewProps) {
  const { open } = useSources();
  const documents = useMemo(() => collectDocuments(messages), [messages]);
  const completedMessages = messages.filter(
    (message) => message.status !== 'streaming' && message.content.trim(),
  ).length;
  const responseInProgress = messages.some((message) => message.status === 'streaming');

  return (
    <section className={styles.panel} role="dialog" aria-label={`Overview of ${title}`}>
      <div className={styles.hero}>
        <span className={styles.heroIcon}><MessageSquareText size={19} /></span>
        <div>
          <h2>Chat overview</h2>
          <p>{completedMessages} messages · {documents.length} documents used</p>
        </div>
      </div>

      <div className={styles.section}>
        <div className={styles.sectionHeader}>
          <div><Sparkles size={15} /><h3>Whole-chat summary</h3></div>
          {summary && !summaryLoading && (
            <button
              type="button"
              className={styles.refresh}
              onClick={onSummarize}
              disabled={responseInProgress}
            >
              <RefreshCw size={13} /> Refresh
            </button>
          )}
        </div>
        {summaryLoading ? (
          <div className={styles.loading} role="status">
            <LoaderCircle size={18} className={styles.spin} />
            Reading the conversation…
          </div>
        ) : summary ? (
          <div className={styles.summary}><MarkdownRenderer content={summary} /></div>
        ) : (
          <div className={styles.summaryEmpty}>
            <p>Generate a concise overview of the entire conversation, including decisions and open questions.</p>
            <button
              type="button"
              onClick={onSummarize}
              disabled={!completedMessages || !onSummarize || responseInProgress}
            >
              <Sparkles size={15} /> Summarize this chat
            </button>
          </div>
        )}
        {summaryError && <p className={styles.error} role="alert">{summaryError}</p>}
      </div>

      <div className={styles.section}>
        <div className={styles.sectionHeader}>
          <div><FileText size={15} /><h3>Documents used</h3></div>
          <span className={styles.count}>{documents.length}</span>
        </div>
        {documents.length ? (
          <ul className={styles.documents}>
            {documents.map((document) => (
              <li key={document.key}>
                <button
                  type="button"
                  onClick={() => {
                    open(document.source);
                    onClose?.();
                  }}
                >
                  <span className={styles.fileIcon}><FileText size={16} /></span>
                  <span className={styles.documentText}>
                    <strong>{document.name}</strong>
                    <small>
                      {document.pages.length ? document.pages.join(', ') : 'Cited text'}
                      {' · '}{document.references} {document.references === 1 ? 'citation' : 'citations'}
                    </small>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.noDocuments}>No documents have been cited in this chat.</p>
        )}
      </div>
    </section>
  );
}

function collectDocuments(messages: Message[]): UsedDocument[] {
  const found = new Map<string, UsedDocument & { pageSet: Set<string> }>();
  for (const message of messages) {
    for (const source of message.sources ?? []) {
      const key = source.docId || source.sourceUri || source.name;
      const current = found.get(key);
      if (current) {
        current.references += 1;
        if (source.page) current.pageSet.add(source.page);
        continue;
      }
      const pageSet = new Set<string>();
      if (source.page) pageSet.add(source.page);
      found.set(key, {
        key,
        name: source.name,
        pageSet,
        pages: [],
        references: 1,
        source,
      });
    }
  }
  return [...found.values()].map(({ pageSet, ...document }) => ({
    ...document,
    pages: [...pageSet],
  }));
}
