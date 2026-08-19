import { ChevronDown, FolderOpen, MessagesSquare, PanelLeft, Plus } from 'lucide-react';
import type { Conversation } from '../../../../types';
import { Button } from '../../../../shared/ui/Button';
import { IconButton } from '../../../../shared/ui/IconButton';
import { Avatar } from '../../../../shared/ui/Avatar';
import { AccountMenu } from '../../../account/components/AccountMenu';
import { ConversationList } from '../ConversationList';
import styles from './HistorySidebar.module.css';

export type AppView = 'chat' | 'documents';

interface HistorySidebarProps {
 conversations: Conversation[];
 activeId: string | null;
 userName: string;
 userInitials: string;
 view: AppView;
 documentsCount: number;
 onSelect: (id: string) => void;
 onNewChat: () => void;
 onNavigate: (view: AppView) => void;
 onToggleCollapse?: () => void;
 onRename?: (id: string, title: string) => void;
 onTogglePin?: (id: string) => void;
 onDelete?: (id: string) => void;
 onSwitchAccount?: () => void;
 onMemories?: () => void;
 onDeletedConversations?: () => void;
 onLogout?: () => void;
}

export function HistorySidebar({
 conversations,
 activeId,
 userName,
 userInitials,
 view,
 documentsCount,
 onSelect,
 onNewChat,
 onNavigate,
 onToggleCollapse,
 onRename,
 onTogglePin,
 onDelete,
 onSwitchAccount,
 onMemories,
 onDeletedConversations,
 onLogout,
}: HistorySidebarProps) {
 return (
   <div className={styles.sidebar}>
     <div className={styles.top}>
       <Button fullWidth leftIcon={<Plus size={16} />} onClick={onNewChat}>
         New chat
       </Button>
       <IconButton label="Collapse sidebar" onClick={onToggleCollapse}>
         <PanelLeft size={18} />
       </IconButton>
     </div>
     <nav className={styles.nav}>
       <button
         type="button"
         className={view === 'chat' ? styles.navItemActive : styles.navItem}
         onClick={() => onNavigate('chat')}
       >
         <MessagesSquare size={16} />
         <span>Trò chuyện</span>
       </button>
       <button
         type="button"
         className={view === 'documents' ? styles.navItemActive : styles.navItem}
         onClick={() => onNavigate('documents')}
       >
         <FolderOpen size={16} />
         <span>Tài liệu</span>
         {documentsCount > 0 && <span className={styles.count}>{documentsCount}</span>}
       </button>
     </nav>
     <div className={styles.divider} />
     <div className={styles.scroll}>
       <ConversationList
         conversations={conversations}
         activeId={activeId}
         onSelect={onSelect}
         onRename={onRename}
         onTogglePin={onTogglePin}
         onDelete={onDelete}
       />
     </div>
     <AccountMenu placement="top" onSwitchAccount={onSwitchAccount} onMemories={onMemories} onDeletedConversations={onDeletedConversations} onLogout={onLogout}>
       {(open) => (
         <button type="button" className={styles.profile} onClick={open} aria-haspopup="menu">
           <Avatar initials={userInitials} size={28} />
           <span className={styles.name}>{userName}</span>
           <ChevronDown size={16} className={styles.profileChevron} />
         </button>
       )}
     </AccountMenu>
   </div>
 );
}
