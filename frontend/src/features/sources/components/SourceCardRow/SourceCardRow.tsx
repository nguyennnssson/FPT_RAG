import type { Source } from '../../../../types';
import { DocumentCard } from '../DocumentCard';
import styles from './SourceCardRow.module.css';

interface SourceCardRowProps {
  sources: Source[];
}

export function SourceCardRow({ sources }: SourceCardRowProps) {
  if (!sources.length) return null;
  return (
    <div className={styles.row} aria-label="Referenced documents">
      {sources.map((s) => (
        <DocumentCard key={s.id} source={s} />
      ))}
    </div>
  );
}
