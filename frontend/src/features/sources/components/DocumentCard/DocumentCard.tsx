import { FileText } from 'lucide-react';
import type { Source } from '../../../../types';
import { useSources } from '../../context';
import styles from './DocumentCard.module.css';

interface DocumentCardProps {
  source: Source;
}

export function DocumentCard({ source }: DocumentCardProps) {
  const { open } = useSources();
  const citationNumber = source.id.replace(/^S/i, '');
  return (
    <button
      type="button"
      className={styles.card}
      onClick={() => open(source)}
      title={`Source ${citationNumber}: ${source.name}`}
    >
      <span className={styles.number} aria-label={`Source ${citationNumber}`}>
        {citationNumber}
      </span>
      <FileText size={15} className={styles.icon} />
      <span className={styles.name}>{source.name}</span>
    </button>
  );
}
