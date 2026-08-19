import { useEffect, useState } from 'react';
import { History, RotateCcw } from 'lucide-react';
import type { Conversation } from '../../../../types';
import {
  listDeletedConversations,
  restoreConversation,
  type UserContext,
} from '../../../../shared/api/client';
import { Modal } from '../../../../shared/ui/Modal';
import { Button } from '../../../../shared/ui/Button';
import styles from './DeletedConversationsDialog.module.css';

interface DeletedConversationsDialogProps {
  open: boolean;
  onClose: () => void;
  onRestore: (conversation: Conversation) => void;
  user: UserContext;
}

export function DeletedConversationsDialog({
  open,
  onClose,
  onRestore,
  user,
}: DeletedConversationsDialogProps) {
  const [items, setItems] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError('');
    void listDeletedConversations(user)
      .then(setItems)
      .catch(() => setError('Không thể tải các hội thoại đã xoá.'))
      .finally(() => setLoading(false));
  }, [open, user]);

  const restore = async (id: string) => {
    try {
      const conversation = await restoreConversation(id, user);
      setItems((current) => current.filter((item) => item.id !== id));
      onRestore(conversation);
      setError('');
    } catch {
      setError('Không thể khôi phục hội thoại.');
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Đã xoá gần đây" width={560}>
      <p className={styles.help}>Hội thoại sẽ bị xoá vĩnh viễn sau ngày hiển thị bên dưới.</p>
      {error && <p className={styles.error}>{error}</p>}
      {loading ? (
        <p className={styles.empty}>Đang tải…</p>
      ) : items.length === 0 ? (
        <div className={styles.empty}><History size={24} />Không có hội thoại nào đang chờ xoá.</div>
      ) : (
        <ul className={styles.list}>
          {items.map((conversation) => (
            <li key={conversation.id}>
              <div>
                <strong>{conversation.title}</strong>
                <small>
                  Xoá vĩnh viễn: {conversation.purgeAfter
                    ? new Date(conversation.purgeAfter).toLocaleDateString('vi-VN')
                    : 'sau 30 ngày'}
                </small>
              </div>
              <Button leftIcon={<RotateCcw size={15} />} onClick={() => void restore(conversation.id)}>
                Khôi phục
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
