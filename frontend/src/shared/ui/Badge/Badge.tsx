import type { ReactNode } from 'react';
import styles from './Badge.module.css';

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

interface BadgeProps {
  tone?: Tone;
  leftIcon?: ReactNode;
  children: ReactNode;
}

export function Badge({ tone = 'neutral', leftIcon, children }: BadgeProps) {
  return (
    <span className={[styles.badge, styles[tone]].join(' ')}>
      {leftIcon && <span className={styles.icon}>{leftIcon}</span>}
      {children}
    </span>
  );
}
