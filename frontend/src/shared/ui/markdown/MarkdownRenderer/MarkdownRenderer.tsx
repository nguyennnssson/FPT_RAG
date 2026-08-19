import { Children, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from '../CodeBlock';
import { InlineCitation } from '../InlineCitation';
import styles from './MarkdownRenderer.module.css';

interface MarkdownRendererProps {
  content: string;
  /** Called with the citation index (as a string) when a [[cite:N]] chip is clicked. */
  onCitation?: (index: string) => void;
}

const CITE = /(\[\[cite:[\w-]+\]\])/g;

/** Replace [[cite:N]] tokens inside text nodes with clickable citation chips. */
function withCitations(children: ReactNode, onCitation?: (index: string) => void): ReactNode {
  return Children.toArray(children).flatMap((node, i): ReactNode[] => {
    if (typeof node !== 'string') return [node];
    return node.split(CITE).map((part, j) => {
      const match = part.match(/^\[\[cite:([\w-]+)\]\]$/);
      if (!match) return part;
      const idx = match[1];
      return (
        <InlineCitation
          key={`c-${i}-${j}`}
          label={idx}
          title={`Source ${idx}`}
          onClick={() => onCitation?.(idx)}
        />
      );
    });
  });
}

export function MarkdownRenderer({ content, onCitation }: MarkdownRendererProps) {
  return (
    <div className={styles.prose}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p>{withCitations(children, onCitation)}</p>,
          li: ({ children }) => <li>{withCitations(children, onCitation)}</li>,
          td: ({ children }) => <td>{withCitations(children, onCitation)}</td>,
          th: ({ children }) => <th>{withCitations(children, onCitation)}</th>,
          strong: ({ children }) => <strong>{withCitations(children, onCitation)}</strong>,
          em: ({ children }) => <em>{withCitations(children, onCitation)}</em>,
          del: ({ children }) => <del>{withCitations(children, onCitation)}</del>,
          h1: ({ children }) => <h1>{withCitations(children, onCitation)}</h1>,
          h2: ({ children }) => <h2>{withCitations(children, onCitation)}</h2>,
          h3: ({ children }) => <h3>{withCitations(children, onCitation)}</h3>,
          h4: ({ children }) => <h4>{withCitations(children, onCitation)}</h4>,
          blockquote: ({ children }) => (
            <blockquote>{withCitations(children, onCitation)}</blockquote>
          ),
          pre: ({ children }) => <>{children}</>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div className={styles.tableWrap}>
              <table>{children}</table>
            </div>
          ),
          code: ({ className, children }) => {
            const text = String(children ?? '');
            const isBlock = text.includes('\n') || /language-/.test(className || '');
            if (isBlock) {
              const language = (className || '').replace('language-', '') || undefined;
              return <CodeBlock code={text.replace(/\n$/, '')} language={language} />;
            }
            return <code className={styles.inlineCode}>{children}</code>;
          },
        }}
      >
        {content}
      </Markdown>
    </div>
  );
}
