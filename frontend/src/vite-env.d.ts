/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the RAG backend. Defaults to '/api' (proxied by Vite in dev). */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
