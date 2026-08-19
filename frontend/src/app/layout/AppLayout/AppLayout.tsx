import type { ReactNode } from 'react';
import styles from './AppLayout.module.css';

interface AppLayoutProps {
  sidebar: ReactNode;
  children: ReactNode;
  view: 'chat' | 'documents';
  /** Mobile off-canvas state. On desktop the sidebar is always visible. */
  sidebarOpen: boolean;
  sidebarCollapsed: boolean;
  onCloseSidebar: () => void;
}

export function AppLayout({
  sidebar,
  children,
  view,
  sidebarOpen,
  sidebarCollapsed,
  onCloseSidebar,
}: AppLayoutProps) {
  return (
    <div className={styles.app}>
      <aside
        className={styles.sidebar}
        data-open={sidebarOpen}
        data-collapsed={sidebarCollapsed}
      >
        {sidebar}
      </aside>
      <div
        className={styles.scrim}
        data-open={sidebarOpen}
        onClick={onCloseSidebar}
        aria-hidden
      />
      <main className={styles.main} data-view={view}>
        {children}
      </main>
    </div>
  );
}
