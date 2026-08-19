import { useCallback, useEffect, useState } from 'react';
import type { UploadedDocument, UploadResult } from '../../../types';
import {
  getDocumentPreview,
  getDocumentFile,
  listDocuments,
  uploadDocument,
  type BackendDocument,
  type UserContext,
} from '../../../shared/api/client';

function fileType(name: string): string {
  const match = /\.([A-Za-z0-9]+)$/.exec(name);
  return match ? match[1].toUpperCase() : 'OTHER';
}

function fileTypeFromDocument(document: BackendDocument): string {
  if (document.file_type) return document.file_type;
  const sourceType = fileType(document.source_uri);
  return sourceType === 'OTHER' ? fileType(document.title) : sourceType;
}

function folderFromSource(sourceUri: string): string {
  let decoded = sourceUri || '';
  try {
    decoded = decodeURIComponent(decoded);
  } catch {
    // Keep a malformed-but-displayable source URI usable for legacy records.
  }
  const normalized = decoded.replace(/\\/g, '/');
  const schemeMatch = /^([a-z][a-z0-9+.-]*):\/\//i.exec(normalized);
  const scheme = schemeMatch?.[1]?.toLowerCase();
  if (scheme === 'upload') return 'Uploads';
  if (scheme === 'api') return 'API';

  const withoutScheme = normalized.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '');
  const parts = withoutScheme.split('/').filter((part) => part && part !== '..' && part !== '.');
  return parts.length > 1 ? parts.at(-2) || 'Other' : 'Other';
}

/** Backend doc_id must match ^[A-Za-z0-9._-]+$ (api/schemas.py); mirror the
 *  server's _doc_id_from_filename so optimistic rows key to the real doc. */
function toDocId(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? name;
  const safe = base.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
  return (safe || 'document').slice(0, 256);
}

function fromBackend(d: BackendDocument): UploadedDocument {
  return {
    id: d.doc_id,
    name: d.title || d.doc_id,
    folder: d.folder || folderFromSource(d.source_uri),
    fileType: fileTypeFromDocument(d),
    size: 0,
    status: d.status || 'active',
    chunks: d.chunks,
    indexedAt: d.indexed_at,
    meta: `${d.chunks} chunks`,
  };
}

/** How many files embed concurrently. The embed step is the slow part on a CPU
 *  backend, so keep this modest to avoid overwhelming the server. */
const UPLOAD_CONCURRENCY = 3;

/**
 * Uploaded-document list, backed by the RAG index. On mount it loads the real
 * documents from GET /api/documents. Uploads go to POST /api/upload as multipart
 * (binary-capable: .pdf/.docx are extracted + embedded server-side), with live
 * per-file status and bounded concurrency.
 */
export function useDocuments(user: UserContext) {
  const [documents, setDocuments] = useState<UploadedDocument[]>([]);

  const patch = useCallback((id: string, next: Partial<UploadedDocument>) => {
    setDocuments((prev) => prev.map((d) => (d.id === id ? { ...d, ...next } : d)));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const server = (await listDocuments(user)).map(fromBackend);
      // Keep any rows still mid-upload or failed; server list is authoritative
      // for everything it returns.
      setDocuments((prev) => {
        const serverIds = new Set(server.map((s) => s.id));
        const pending = prev.filter(
          (d) =>
            (d.status === 'processing' || d.status === 'failed') &&
            !serverIds.has(d.id),
        );
        return [...pending, ...server];
      });
    } catch {
      /* backend unavailable — leave the list as-is */
    }
  }, [user]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const addFiles = useCallback(
    async (files: FileList | File[]): Promise<UploadResult[]> => {
      const arr = Array.from(files);
      if (!arr.length) return [];

      // Optimistic rows so the user sees the upload immediately.
      const optimistic: UploadedDocument[] = arr.map((f) => ({
        id: toDocId(f.name),
        name: f.name,
        folder: 'Uploads',
        fileType: fileType(f.name),
        size: f.size,
        status: 'processing',
        indexedAt: new Date().toISOString(),
        meta: 'processing…',
      }));
      setDocuments((prev) => {
        const optimisticIds = new Set(optimistic.map((o) => o.id));
        return [...optimistic, ...prev.filter((d) => !optimisticIds.has(d.id))];
      });

      // Bounded-concurrency worker pool over the file list.
      let cursor = 0;
      const results: UploadResult[] = new Array(arr.length);
      const worker = async () => {
        while (cursor < arr.length) {
          const index = cursor++;
          const f = arr[index];
          const id = toDocId(f.name);
          patch(id, { status: 'processing', meta: 'processing…' });
          try {
            const res = await uploadDocument(f, {}, user);
            patch(id, {
              status: 'active',
              chunks: res.chunks_indexed,
              meta: `${res.chunks_indexed} chunks`,
              error: undefined,
            });
            results[index] = {
              fileName: f.name,
              docId: res.doc_id,
              status: 'active',
              chunks: res.chunks_indexed,
            };
          } catch (err) {
            const error = err instanceof Error ? err.message : String(err);
            patch(id, {
              status: 'failed',
              meta: 'failed',
              error,
            });
            results[index] = {
              fileName: f.name,
              docId: id,
              status: 'failed',
              error,
            };
          }
        }
      };
      const pool = Array.from({ length: Math.min(UPLOAD_CONCURRENCY, arr.length) }, worker);
      await Promise.all(pool);
      await refresh();
      return results;
    },
    [patch, refresh, user],
  );

  const previewDocument = useCallback(
    (id: string) => getDocumentPreview(id, user),
    [user],
  );

  const previewDocumentFile = useCallback(
    (id: string) => getDocumentFile(id, user),
    [user],
  );

  return { documents, addFiles, previewDocument, previewDocumentFile };
}
