import type { KeyboardEvent } from 'react';
import styles from './InlineCitation.module.css';

interface InlineCitationProps {
  label: string | number;
  title?: string;
  onClick?: () => void;
}

export function InlineCitation({ label, title, onClick }: InlineCitationProps) {
  const onKey = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onClick?.();
    }
  };
  return (
    <sup
      className={styles.cite}
      role="button"
      tabIndex={0}
      title={title}
      onClick={onClick}
      onKeyDown={onKey}
    >
      {label}
    </sup>
  );
}
