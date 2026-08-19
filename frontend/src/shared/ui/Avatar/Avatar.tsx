import styles from './Avatar.module.css';

interface AvatarProps {
  initials: string;
  size?: number;
  onClick?: () => void;
}

export function Avatar({ initials, size = 30, onClick }: AvatarProps) {
  const style = { width: size, height: size, fontSize: Math.round(size * 0.42) };
  return onClick ? (
    <button type="button" className={styles.avatar} style={style} onClick={onClick} aria-label="Account">
      {initials}
    </button>
  ) : (
    <span className={styles.avatar} style={style} aria-hidden>
      {initials}
    </span>
  );
}
