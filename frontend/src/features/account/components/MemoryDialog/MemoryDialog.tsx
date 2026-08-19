import { useEffect, useState } from 'react';
import { Brain, Plus, Trash2 } from 'lucide-react';
import type { MemoryKind, UserMemory } from '../../../../types';
import {
  createMemory,
  deleteMemory,
  listMemories,
  type UserContext,
} from '../../../../shared/api/client';
import { Modal } from '../../../../shared/ui/Modal';
import { Button } from '../../../../shared/ui/Button';
import styles from './MemoryDialog.module.css';

interface MemoryDialogProps {
  open: boolean;
  onClose: () => void;
  user: UserContext;
}

const LABELS: Record<MemoryKind, string> = {
  preference: 'Sở thích',
  fact: 'Thông tin',
  instruction: 'Chỉ dẫn',
};

export function MemoryDialog({ open, onClose, user }: MemoryDialogProps) {
  const [memories, setMemories] = useState<UserMemory[]>([]);
  const [content, setContent] = useState('');
  const [kind, setKind] = useState<MemoryKind>('preference');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError('');
    void listMemories(user)
      .then(setMemories)
      .catch(() => setError('Không thể tải bộ nhớ.'))
      .finally(() => setLoading(false));
  }, [open, user]);

  const add = async () => {
    const value = content.trim();
    if (!value) return;
    try {
      const created = await createMemory(value, kind, user);
      setMemories((current) => [
        created,
        ...current.filter((memory) => memory.id !== created.id),
      ]);
      setContent('');
      setError('');
    } catch {
      setError('Không thể lưu bộ nhớ này.');
    }
  };

  const remove = async (id: string) => {
    try {
      await deleteMemory(id, user);
      setMemories((current) => current.filter((memory) => memory.id !== id));
    } catch {
      setError('Không thể xoá bộ nhớ.');
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Bộ nhớ của tôi" width={600}>
      <p className={styles.help}>
        Chỉ những điều bạn thêm ở đây hoặc yêu cầu rõ ràng bằng “hãy nhớ…” mới được dùng ở các cuộc trò chuyện khác.
      </p>
      <div className={styles.form}>
        <select value={kind} onChange={(event) => setKind(event.target.value as MemoryKind)}>
          {Object.entries(LABELS).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
        <input
          value={content}
          maxLength={500}
          placeholder="Ví dụ: Trả lời ngắn gọn bằng tiếng Việt"
          onChange={(event) => setContent(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void add();
          }}
        />
        <Button variant="primary" leftIcon={<Plus size={15} />} onClick={() => void add()}>
          Thêm
        </Button>
      </div>
      {error && <p className={styles.error}>{error}</p>}
      {loading ? (
        <p className={styles.empty}>Đang tải…</p>
      ) : memories.length === 0 ? (
        <div className={styles.empty}><Brain size={24} />Chưa có bộ nhớ nào.</div>
      ) : (
        <ul className={styles.list}>
          {memories.map((memory) => (
            <li key={memory.id}>
              <div>
                <span className={styles.kind}>{LABELS[memory.kind]}</span>
                <p>{memory.content}</p>
                <small>{memory.isExplicit ? 'Đã lưu trực tiếp' : 'Bộ nhớ cũ'}</small>
              </div>
              <button type="button" aria-label="Xoá bộ nhớ" onClick={() => void remove(memory.id)}>
                <Trash2 size={16} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
