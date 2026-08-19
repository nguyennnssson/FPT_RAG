import { FileText } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Source } from '../../../../types';
import { Drawer } from '../../../../shared/ui/Drawer';
import { Badge } from '../../../../shared/ui/Badge';
import styles from './SourceDrawer.module.css';

interface SourceDrawerProps {
  source: Source | null;
  onClose: () => void;
}

/** Wrap the highlighted substring of a snippet in a <mark>. */
function renderSnippet(snippet?: string, highlight?: string): ReactNode {
  if (!snippet) return null;
  if (!highlight) return snippet;
  const at = snippet.indexOf(highlight);
  if (at === -1) return snippet;
  return (
    <>
      {snippet.slice(0, at)}
      <mark className={styles.hl}>{highlight}</mark>
      {snippet.slice(at + highlight.length)}
    </>
  );
}

export function SourceDrawer({ source, onClose }: SourceDrawerProps) {
  const citedText = source?.text?.trim();
  const title = (
    <span className={styles.fileTitle}>
      <FileText size={16} className={styles.fileIcon} />
      {source?.name ?? 'Source'}
    </span>
  );

  return (
    <Drawer open={Boolean(source)} onClose={onClose} title={title} width={480}>
      {source && (
        <div className={styles.content}>
          <Badge tone="success">Citation {source.id}</Badge>
          {typeof source.confidence === 'number' && (
            <Badge tone="success">
              Confidence {Math.round(source.confidence * 100)}% · matched chunk
            </Badge>
          )}
          <section className={styles.excerptSection}>
            <h3>Cited text</h3>
            {citedText ? (
              <p className={styles.snippet}>{renderSnippet(citedText, source.highlight)}</p>
            ) : (
              <p className={styles.unavailable}>
                The cited text is unavailable for this older saved message.
              </p>
            )}
          </section>
          <dl className={styles.metadata}>
            {source.page && <><dt>Location</dt><dd>{source.page}</dd></>}
            {source.sectionPath && source.sectionPath.length > 0 && (
              <><dt>Section</dt><dd>{source.sectionPath.join(' › ')}</dd></>
            )}
            {source.sourceUri && <><dt>Source</dt><dd>{source.sourceUri}</dd></>}
          </dl>
        </div>
      )}
    </Drawer>
  );
}
