import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import type { Source } from '../../types';
import { SourceDrawer } from './components/SourceDrawer';

interface SourcesContextValue {
  activeSource: Source | null;
  open: (source: Source) => void;
  close: () => void;
}

const SourcesContext = createContext<SourcesContextValue | null>(null);

export function SourcesProvider({ children }: { children: ReactNode }) {
  const [activeSource, setActiveSource] = useState<Source | null>(null);

  const open = useCallback((source: Source) => setActiveSource(source), []);
  const close = useCallback(() => setActiveSource(null), []);

  const value = useMemo(() => ({ activeSource, open, close }), [activeSource, open, close]);

  return (
    <SourcesContext.Provider value={value}>
      {children}
      <SourceDrawer source={activeSource} onClose={close} />
    </SourcesContext.Provider>
  );
}

export function useSources(): SourcesContextValue {
  const ctx = useContext(SourcesContext);
  if (!ctx) throw new Error('useSources must be used within a SourcesProvider');
  return ctx;
}
