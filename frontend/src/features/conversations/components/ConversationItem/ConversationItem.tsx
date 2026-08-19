import { useRef, useState, type KeyboardEvent, type MouseEvent } from 'react';
import { MoreHorizontal, Pencil, Pin, PinOff, Trash2 } from 'lucide-react';
import type { Conversation } from '../../../../types';
import { Menu, type MenuItem } from '../../../../shared/ui/Menu';
import { ConfirmDialog } from '../../../../shared/ui/ConfirmDialog';
import styles from './ConversationItem.module.css';

interface ConversationItemProps {
 conversation: Conversation;
 active: boolean;
 onSelect: (id: string) => void;
 onRename?: (id: string, title: string) => void;
 onTogglePin?: (id: string) => void;
 onDelete?: (id: string) => void;
}

export function ConversationItem({
 conversation,
 active,
 onSelect,
 onRename,
 onTogglePin,
 onDelete,
}: ConversationItemProps) {
 const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null);
 const [renaming, setRenaming] = useState(false);
 const [confirmOpen, setConfirmOpen] = useState(false);
 const [draft, setDraft] = useState(conversation.title);
 const inputRef = useRef<HTMLInputElement>(null);

 const openMenu = (e: MouseEvent<HTMLButtonElement>) => {
   e.stopPropagation();
   const r = e.currentTarget.getBoundingClientRect();
   const width = 200;
   const x = Math.min(r.left, window.innerWidth - width - 8);
   setMenuPos({ x, y: r.bottom + 4 });
 };

 const startRename = () => {
   setDraft(conversation.title);
   setRenaming(true);
   setTimeout(() => inputRef.current?.select(), 0);
 };

 const commitRename = () => {
   const next = draft.trim();
   if (next && next !== conversation.title) onRename?.(conversation.id, next);
   setRenaming(false);
 };

 const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
   if (e.key === 'Enter') {
     e.preventDefault();
     commitRename();
   }
   if (e.key === 'Escape') {
     e.preventDefault();
     setRenaming(false);
   }
 };

 const items: MenuItem[] = [
   { label: 'Đổi tên', icon: <Pencil size={15} />, onClick: startRename },
   {
     label: conversation.pinned ? 'Bỏ ghim' : 'Ghim',
     icon: conversation.pinned ? <PinOff size={15} /> : <Pin size={15} />,
     onClick: () => onTogglePin?.(conversation.id),
   },
   {
     label: 'Xoá',
     icon: <Trash2 size={15} />,
     danger: true,
     onClick: () => setConfirmOpen(true),
   },
 ];

 if (renaming) {
   return (
     <div className={[styles.item, styles.renaming].join(' ')}>
       <input
         ref={inputRef}
         className={styles.input}
         value={draft}
         autoFocus
         onChange={(e) => setDraft(e.target.value)}
         onBlur={commitRename}
         onKeyDown={onKeyDown}
       />
     </div>
   );
 }

 return (
   <div className={[styles.item, active ? styles.active : ''].filter(Boolean).join(' ')}>
     <button
       type="button"
       className={styles.label}
       onClick={() => onSelect(conversation.id)}
       aria-current={active ? 'true' : undefined}
       title={conversation.title}
     >
       {conversation.pinned && <Pin size={13} className={styles.pin} />}
       <span className={styles.text}>{conversation.title}</span>
     </button>
     <button
       type="button"
       className={styles.menuBtn}
       aria-label="Tùy chọn hội thoại"
       onClick={openMenu}
     >
       <MoreHorizontal size={16} />
     </button>
     <Menu
       open={menuPos !== null}
       x={menuPos?.x ?? 0}
       y={menuPos?.y ?? 0}
       items={items}
       onClose={() => setMenuPos(null)}
     />
     <ConfirmDialog
       open={confirmOpen}
       title="Xoá hội thoại?"
       message={<>Hội thoại “{conversation.title}” sẽ được ẩn ngay và tự động xoá vĩnh viễn sau 30 ngày.</>}
       confirmLabel="Xoá"
       danger
       onConfirm={() => {
         onDelete?.(conversation.id);
         setConfirmOpen(false);
       }}
       onCancel={() => setConfirmOpen(false)}
     />
   </div>
 );
}
