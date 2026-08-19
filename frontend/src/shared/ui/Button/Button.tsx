import type { ButtonHTMLAttributes, ReactNode } from 'react';
import styles from './Button.module.css';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
 variant?: Variant;
 leftIcon?: ReactNode;
 fullWidth?: boolean;
}

export function Button({
 variant = 'secondary',
 leftIcon,
 fullWidth = false,
 className = '',
 children,
 ...rest
}: ButtonProps) {
 const cls = [styles.btn, styles[variant], fullWidth ? styles.full : '', className]
   .filter(Boolean)
   .join(' ');
 return (
   <button className={cls} {...rest}>
     {leftIcon && <span className={styles.icon}>{leftIcon}</span>}
     {children}
   </button>
 );
}