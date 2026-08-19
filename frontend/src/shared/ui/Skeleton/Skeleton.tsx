import styles from './Skeleton.module.css';

interface SkeletonProps {
  width?: number | string;
  height?: number | string;
  radius?: number;
}

export function Skeleton({ width = '100%', height = 16, radius = 6 }: SkeletonProps) {
  return <span className={styles.skeleton} style={{ width, height, borderRadius: radius }} />;
}
