import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from 'react';
import {
  AlertCircle,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  FileText,
  Download,
  Loader2,
  PanelLeft,
  Search,
  Upload,
  X,
} from 'lucide-react';
import type { DocumentStatus, UploadedDocument } from '../../../../types';
import type { DocumentPreview } from '../../../../shared/api/client';
import { Badge } from '../../../../shared/ui/Badge';
import { IconButton } from '../../../../shared/ui/IconButton';
import { Modal } from '../../../../shared/ui/Modal';
import styles from './DocumentsPage.module.css';

const ACCEPT = '.pdf,.docx,.pptx,.txt,.md,.markdown,.rst,.csv,.html,.htm,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff';
const MIN_PAGE_SIZE = 1;
const TABLE_ROW_HEIGHT = 42;
const TABLE_HEADER_HEIGHT = 34;
const LIST_BOTTOM_CLEARANCE = 16;
const ROW_CLIP_GUARD = 6;

const SEARCH_TYPE_OPTIONS = [
  { value: 'folder', label: 'Thư mục' },
  { value: 'name', label: 'Tên' },
  { value: 'fileType', label: 'Loại tệp' },
  { value: 'date', label: 'Ngày' },
  { value: 'status', label: 'Trạng thái' },
] as const;
type SearchType = (typeof SEARCH_TYPE_OPTIONS)[number]['value'];
const DEFAULT_SEARCH_TYPES: SearchType[] = SEARCH_TYPE_OPTIONS.map((option) => option.value);

interface DocumentsPageProps {
  documents: UploadedDocument[];
  onFiles: (files: FileList | File[]) => void;
  onPreview: (id: string) => Promise<DocumentPreview>;
  onPreviewFile: (id: string) => Promise<Blob>;
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
}

function formatDate(iso?: string): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString();
}

function formatDateTime(iso?: string): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function statusLabel(status?: DocumentStatus): string {
  switch (status) {
    case 'processing':
      return 'Đang xử lý';
    case 'failed':
      return 'Thất bại';
    case 'active':
    default:
      return 'Hoạt động';
  }
}

function searchableValue(document: UploadedDocument, type: SearchType): string {
  const fields: Record<SearchType, string> = {
    folder: document.folder,
    name: document.name,
    fileType: document.fileType,
    date: `${document.indexedAt ?? ''} ${formatDate(document.indexedAt)}`,
    status: statusLabel(document.status),
  };
  return fields[type].toLocaleLowerCase();
}

function StatusBadge({ status, error }: { status?: DocumentStatus; error?: string }) {
  switch (status) {
    case 'processing':
      return (
        <Badge tone="info" leftIcon={<Loader2 size={12} className={styles.spin} />}>
          Đang xử lý
        </Badge>
      );
    case 'failed':
      return (
        <span title={error}>
          <Badge tone="danger" leftIcon={<AlertCircle size={12} />}>
            Thất bại
          </Badge>
        </span>
      );
    case 'active':
    default:
      return (
        <Badge tone="success" leftIcon={<CheckCircle2 size={12} />}>
          Hoạt động
        </Badge>
      );
  }
}

export function DocumentsPage({
  documents,
  onFiles,
  onPreview,
  onPreviewFile,
  sidebarCollapsed,
  onToggleSidebar,
}: DocumentsPageProps) {
  const pageRef = useRef<HTMLDivElement>(null);
  const tableViewportRef = useRef<HTMLDivElement>(null);
  const tableRef = useRef<HTMLTableElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const searchTypesRef = useRef<HTMLDetailsElement>(null);
  const previewRequestRef = useRef(0);
  const [dragging, setDragging] = useState(false);
  const [query, setQuery] = useState('');
  const [selectedTypes, setSelectedTypes] = useState<SearchType[]>(DEFAULT_SEARCH_TYPES);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [pageInput, setPageInput] = useState('1');
  const [pageSize, setPageSize] = useState(MIN_PAGE_SIZE);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [preview, setPreview] = useState<DocumentPreview | null>(null);
  const [previewTitle, setPreviewTitle] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [previewFileError, setPreviewFileError] = useState('');
  const [previewFileUrl, setPreviewFileUrl] = useState('');
  const [previewFileType, setPreviewFileType] = useState('');
  const [previewMode, setPreviewMode] = useState<'original' | 'text'>('text');

  const releasePreviewFile = () => {
    setPreviewFileUrl((current) => {
      if (current) URL.revokeObjectURL(current);
      return '';
    });
    setPreviewFileType('');
  };

  useEffect(() => () => {
    if (previewFileUrl) URL.revokeObjectURL(previewFileUrl);
  }, [previewFileUrl]);

  const pick = () => inputRef.current?.click();

  const onChange = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files?.length) onFiles(event.target.files);
    event.target.value = '';
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    if (event.dataTransfer.files?.length) onFiles(event.dataTransfer.files);
  };

  const selectedTypeSet = useMemo(() => new Set(selectedTypes), [selectedTypes]);
  const textSearchTypes = useMemo(
    () => selectedTypes.filter((type) => type !== 'date'),
    [selectedTypes],
  );

  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const fromTime = dateFrom ? new Date(`${dateFrom}T00:00:00`).getTime() : null;
    const toTime = dateTo ? new Date(`${dateTo}T23:59:59.999`).getTime() : null;

    return documents.filter((document) => {
      if (
        normalizedQuery &&
        textSearchTypes.length > 0 &&
        !textSearchTypes.some((type) =>
          searchableValue(document, type).includes(normalizedQuery),
        )
      ) {
        return false;
      }

      if (selectedTypeSet.has('date') && (fromTime !== null || toTime !== null)) {
        const documentTime = document.indexedAt ? new Date(document.indexedAt).getTime() : NaN;
        if (Number.isNaN(documentTime)) return false;
        if (fromTime !== null && documentTime < fromTime) return false;
        if (toTime !== null && documentTime > toTime) return false;
      }

      return true;
    });
  }, [
    dateFrom,
    dateTo,
    documents,
    query,
    selectedTypeSet,
    textSearchTypes,
  ]);

  const selectedTypeSummary = useMemo(() => {
    if (selectedTypes.length === SEARCH_TYPE_OPTIONS.length) return 'Tất cả 5 trường';
    if (selectedTypes.length === 1) {
      return SEARCH_TYPE_OPTIONS.find((option) => option.value === selectedTypes[0])?.label;
    }
    return `${selectedTypes.length} trường`;
  }, [selectedTypes]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const pagedDocuments = useMemo(
    () => filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize),
    [currentPage, filtered, pageSize],
  );

  useEffect(() => {
    const closeSearchTypes = (event: MouseEvent) => {
      if (!searchTypesRef.current?.contains(event.target as Node)) {
        searchTypesRef.current?.removeAttribute('open');
      }
    };
    document.addEventListener('mousedown', closeSearchTypes);
    return () => document.removeEventListener('mousedown', closeSearchTypes);
  }, []);

  useLayoutEffect(() => {
    const tableViewport = tableViewportRef.current;
    if (!tableViewport) return;

    const updatePageSize = () => {
      const availableHeight = tableViewport.getBoundingClientRect().height;
      const renderedHeaderHeight =
        tableRef.current?.tHead?.getBoundingClientRect().height ?? TABLE_HEADER_HEIGHT;
      const renderedRowHeight =
        tableRef.current?.tBodies[0]?.rows[0]?.getBoundingClientRect().height ??
        TABLE_ROW_HEIGHT;
      const rowsThatFit = Math.floor(
        (availableHeight -
          renderedHeaderHeight -
          LIST_BOTTOM_CLEARANCE -
          ROW_CLIP_GUARD) /
          renderedRowHeight,
      );
      const nextPageSize = Math.max(MIN_PAGE_SIZE, rowsThatFit);
      setPageSize((currentSize) =>
        currentSize === nextPageSize ? currentSize : nextPageSize,
      );
    };

    updatePageSize();
    const resizeObserver = new ResizeObserver(updatePageSize);
    resizeObserver.observe(tableViewport);
    window.addEventListener('resize', updatePageSize);

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener('resize', updatePageSize);
    };
  }, [documents.length, filtered.length]);

  const goToPage = (page: number) => {
    const next = Math.min(totalPages, Math.max(1, page));
    setCurrentPage(next);
    setPageInput(String(next));
  };

  const commitPageInput = () => {
    const parsed = Number.parseInt(pageInput, 10);
    goToPage(Number.isFinite(parsed) ? parsed : currentPage);
  };

  useEffect(() => {
    setCurrentPage(1);
    setPageInput('1');
  }, [dateFrom, dateTo, query, selectedTypes]);

  useEffect(() => {
    if (currentPage > totalPages) goToPage(totalPages);
  }, [currentPage, totalPages]);

  const toggleSearchType = (type: SearchType) => {
    setSelectedTypes((current) => {
      if (current.includes(type)) {
        const next = current.filter((item) => item !== type);
        return next.length ? next : DEFAULT_SEARCH_TYPES;
      }
      return SEARCH_TYPE_OPTIONS
        .map((option) => option.value)
        .filter((item) => current.includes(item) || item === type);
    });
  };

  const openPreview = async (document: UploadedDocument) => {
    const requestId = previewRequestRef.current + 1;
    previewRequestRef.current = requestId;
    setPreviewTitle(document.name);
    setPreview(null);
    setPreviewError('');
    setPreviewFileError('');
    releasePreviewFile();
    setPreviewMode('text');
    setPreviewLoading(true);
    setPreviewOpen(true);
    try {
      const result = await onPreview(document.id);
      if (previewRequestRef.current !== requestId) return;
      setPreview(result);
      if (result.has_original) {
        try {
          const blob = await onPreviewFile(document.id);
          const url = URL.createObjectURL(blob);
          if (previewRequestRef.current !== requestId) {
            URL.revokeObjectURL(url);
            return;
          }
          setPreviewFileUrl(url);
          setPreviewFileType(blob.type || result.content_type);
          if ((blob.type || result.content_type) === 'application/pdf'
              || (blob.type || result.content_type).startsWith('image/')) {
            setPreviewMode('original');
          }
        } catch {
          if (previewRequestRef.current === requestId) {
            setPreviewFileError('Không thể tải tệp gốc; vẫn hiển thị văn bản đã trích xuất.');
          }
        }
      }
    } catch {
      if (previewRequestRef.current === requestId) {
        setPreviewError('Không thể tải bản xem trước của tài liệu này.');
      }
    } finally {
      if (previewRequestRef.current === requestId) setPreviewLoading(false);
    }
  };

  const closePreview = () => {
    previewRequestRef.current += 1;
    releasePreviewFile();
    setPreviewOpen(false);
  };

  const activeCount = documents.filter((document) => document.status === 'active').length;
  const filtersActive = Boolean(query || dateFrom || dateTo);

  return (
    <div ref={pageRef} className={styles.page}>
      <IconButton
        label="Mở thanh bên"
        className={[
          styles.sidebarButton,
          sidebarCollapsed ? styles.sidebarButtonVisible : '',
        ]
          .filter(Boolean)
          .join(' ')}
        onClick={onToggleSidebar}
      >
        <PanelLeft size={18} />
      </IconButton>

      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>Tài liệu</h1>
          <p className={styles.subtitle}>
            {documents.length} tài liệu · {activeCount} đang hoạt động
          </p>
        </div>
      </header>

      <div
        className={[styles.dropzone, dragging ? styles.dragging : ''].filter(Boolean).join(' ')}
        onClick={pick}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            pick();
          }
        }}
        aria-label="Tải tài liệu lên"
      >
        <Upload size={22} className={styles.upIcon} />
        <div className={styles.dropText}>
          <strong>Kéo thả tài liệu vào đây</strong> hoặc bấm để chọn tệp
        </div>
        <div className={styles.hint}>
          Hỗ trợ PDF, DOCX, PPTX, ảnh, TXT, MD, CSV, HTML · tối đa 25 MB/tệp
        </div>
        <input
          ref={inputRef}
          type="file"
          multiple
          className={styles.input}
          onChange={onChange}
          accept={ACCEPT}
        />
      </div>

      <div className={styles.filterArea}>
        <div className={styles.searchRow}>
          <details ref={searchTypesRef} className={styles.typeSelect}>
            <summary>
              <span>Loại tìm kiếm: {selectedTypeSummary}</span>
              <ChevronDown size={15} />
            </summary>
            <div className={styles.typeMenu}>
              <div className={styles.typeMenuHeader}>Tìm trong nhiều trường</div>
              {SEARCH_TYPE_OPTIONS.map((option) => (
                <label key={option.value} className={styles.typeOption}>
                  <input
                    type="checkbox"
                    checked={selectedTypeSet.has(option.value)}
                    onChange={() => toggleSearchType(option.value)}
                  />
                  <span>{option.label}</span>
                </label>
              ))}
              <button
                type="button"
                className={styles.selectAll}
                onClick={() => setSelectedTypes(DEFAULT_SEARCH_TYPES)}
              >
                Chọn tất cả
              </button>
            </div>
          </details>

          <div className={styles.searchField}>
            <Search size={16} className={styles.searchIcon} />
            <input
              className={styles.searchInput}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={
                textSearchTypes.length
                  ? 'Tìm theo các trường đã chọn…'
                  : 'Chọn trường văn bản để tìm…'
              }
              disabled={textSearchTypes.length === 0}
              aria-label="Tìm tài liệu"
            />
            {query && (
              <IconButton label="Xoá" className={styles.clear} onClick={() => setQuery('')}>
                <X size={14} />
              </IconButton>
            )}
          </div>
        </div>

        {selectedTypeSet.has('date') && (
          <div className={styles.dateFilters} aria-label="Khoảng ngày">
            <CalendarDays size={16} />
            <label>
              <span>Từ</span>
              <input
                type="date"
                value={dateFrom}
                max={dateTo || undefined}
                onChange={(event) => setDateFrom(event.target.value)}
              />
            </label>
            <label>
              <span>Đến</span>
              <input
                type="date"
                value={dateTo}
                min={dateFrom || undefined}
                onChange={(event) => setDateTo(event.target.value)}
              />
            </label>
            {filtersActive && (
              <button
                type="button"
                className={styles.resetFilters}
                onClick={() => {
                  setQuery('');
                  setDateFrom('');
                  setDateTo('');
                }}
              >
                Xoá bộ lọc
              </button>
            )}
          </div>
        )}
      </div>

      <div className={styles.tableWrap}>
        <div ref={tableViewportRef} className={styles.tableViewport}>
          {documents.length === 0 ? (
            <div className={styles.empty}>
              Chưa có tài liệu nào. Tải tệp lên để bắt đầu.
            </div>
          ) : (
            <table ref={tableRef} className={styles.table}>
              <colgroup>
                <col className={styles.folderColumn} />
                <col />
                <col className={styles.typeColumn} />
                <col className={styles.statusColumn} />
                <col className={styles.dateColumn} />
              </colgroup>
              <thead>
                <tr>
                  <th>Thư mục</th>
                  <th>Tên</th>
                  <th className={styles.colType}>Loại tệp</th>
                  <th className={styles.colStatus}>Trạng thái</th>
                  <th className={styles.colDate}>Ngày</th>
                </tr>
              </thead>
              <tbody>
                {pagedDocuments.map((document) => (
                  <tr key={document.id}>
                    <td className={styles.folderCell} title={document.folder}>
                      {document.folder}
                    </td>
                    <td className={styles.nameCell} title={document.name}>
                      <div className={styles.nameContent}>
                        <FileText size={15} className={styles.fileIcon} />
                        <button
                          type="button"
                          className={styles.name}
                          disabled={document.status !== 'active'}
                          onClick={() => void openPreview(document)}
                          aria-label={`Xem trước ${document.name}`}
                        >
                          {document.name}
                        </button>
                      </div>
                    </td>
                    <td className={styles.colType}>{document.fileType}</td>
                    <td className={styles.colStatus}>
                      <StatusBadge status={document.status} error={document.error} />
                    </td>
                    <td
                      className={styles.colDate}
                      title={formatDateTime(document.indexedAt)}
                    >
                      {formatDate(document.indexedAt)}
                    </td>
                  </tr>
                ))}
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={5} className={styles.empty}>
                      Không có tài liệu phù hợp với bộ lọc hiện tại.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
        <nav className={styles.pagination} aria-label="Phân trang tài liệu">
          <button
            type="button"
            className={styles.pageButton}
            onClick={() => goToPage(1)}
            disabled={currentPage === 1}
            aria-label="Trang đầu"
          >
            {'<<'}
          </button>
          <button
            type="button"
            className={styles.pageButton}
            onClick={() => goToPage(currentPage - 1)}
            disabled={currentPage === 1}
            aria-label="Trang trước"
          >
            {'<'}
          </button>
          <input
            className={styles.pageInput}
            type="number"
            min={1}
            max={totalPages}
            value={pageInput}
            onChange={(event) => setPageInput(event.target.value)}
            onBlur={commitPageInput}
            onKeyDown={(event) => {
              if (event.key === 'Enter') event.currentTarget.blur();
            }}
            aria-label="Trang hiện tại"
          />
          <span className={styles.pageTotal}>/ {totalPages}</span>
          <button
            type="button"
            className={styles.pageButton}
            onClick={() => goToPage(currentPage + 1)}
            disabled={currentPage === totalPages}
            aria-label="Trang sau"
          >
            {'>'}
          </button>
          <button
            type="button"
            className={styles.pageButton}
            onClick={() => goToPage(totalPages)}
            disabled={currentPage === totalPages}
            aria-label="Trang cuối"
          >
            {'>>'}
          </button>
        </nav>
      </div>

      <Modal open={previewOpen} onClose={closePreview} title={previewTitle} width={780}>
        {previewLoading ? (
          <div className={styles.previewState}>
            <Loader2 size={20} className={styles.spin} />
            Đang tải bản xem trước…
          </div>
        ) : previewError ? (
          <div className={styles.previewError}>
            <AlertCircle size={18} />
            {previewError}
          </div>
        ) : preview ? (
          <div className={styles.preview}>
            <div className={styles.previewMeta}>
              <span>{preview.folder}</span>
              <span>{preview.file_type}</span>
              <span>{formatDateTime(preview.indexed_at)}</span>
            </div>
            <div className={styles.previewToolbar}>
              <div className={styles.previewTabs}>
                {previewFileUrl && (previewFileType === 'application/pdf' || previewFileType.startsWith('image/')) && (
                  <button
                    type="button"
                    className={previewMode === 'original' ? styles.previewTabActive : styles.previewTab}
                    onClick={() => setPreviewMode('original')}
                  >
                    Bản gốc
                  </button>
                )}
                <button
                  type="button"
                  className={previewMode === 'text' ? styles.previewTabActive : styles.previewTab}
                  onClick={() => setPreviewMode('text')}
                >
                  Văn bản đã trích xuất
                </button>
              </div>
              {previewFileUrl && (
                <a className={styles.downloadOriginal} href={previewFileUrl} download={preview.file_name}>
                  <Download size={15} />
                  Tải bản gốc
                </a>
              )}
            </div>
            {previewFileError && <div className={styles.previewFileError}>{previewFileError}</div>}
            {previewMode === 'original' && previewFileUrl ? (
              previewFileType.startsWith('image/') ? (
                <div className={styles.originalImageWrap}>
                  <img className={styles.originalImage} src={previewFileUrl} alt={preview.file_name} />
                </div>
              ) : (
                <iframe
                  className={styles.pdfPreview}
                  src={previewFileUrl}
                  title={`Bản xem trước ${preview.file_name}`}
                />
              )
            ) : (
              <>
                <div className={styles.previewNote}>
                  Đây là nội dung được OCR/trích xuất và dùng cho tìm kiếm RAG.
                </div>
                <pre className={styles.previewContent}>{preview.content || 'Tài liệu không có nội dung để xem trước.'}</pre>
                {preview.truncated && (
                  <p className={styles.previewTruncated}>
                    Bản xem trước đã được rút gọn vì tài liệu quá dài.
                  </p>
                )}
              </>
            )}
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
