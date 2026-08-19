import { useMemo } from 'react';
import type { Conversation } from '../../../../types';
import { ConversationItem } from '../ConversationItem';
import { groupByDate } from './groupByDate';
import styles from './ConversationList.module.css';

interface ConversationListProps {
 conversations: Conversation[];
 activeId: string | null;
 onSelect: (id: string) => void;
 onRename?: (id: string, title: string) => void;
 onTogglePin?: (id: string) => void;
 onDelete?: (id: string) => void;
}

export function ConversationList({
 conversations,
 activeId,
 onSelect,
 onRename,
 onTogglePin,
 onDelete,
}: ConversationListProps) {
 const groups = useMemo(() => {
   const pinned = conversations.filter((c) => c.pinned);
   const rest = conversations.filter((c) => !c.pinned);
   return [
     ...(pinned.length ? [{ label: 'Đã ghim', items: pinned }] : []),
     ...groupByDate(rest),
   ];
 }, [conversations]);

 return (
   <nav className={styles.list} aria-label="Conversation history">
     {groups.map((group) => (
       <div key={group.label} className={styles.group}>
         <div className={styles.label}>{group.label}</div>
         {group.items.map((c) => (
           <ConversationItem
             key={c.id}
             conversation={c}
             active={c.id === activeId}
             onSelect={onSelect}
             onRename={onRename}
             onTogglePin={onTogglePin}
             onDelete={onDelete}
           />
         ))}
       </div>
     ))}
   </nav>
 );
}