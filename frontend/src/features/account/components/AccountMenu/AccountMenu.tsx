import { useState, type MouseEvent, type ReactNode } from 'react';
import { Brain, History, LogOut, Users } from 'lucide-react';
import { Menu, type MenuItem } from '../../../../shared/ui/Menu';

interface AccountMenuProps {
 placement?: 'top' | 'bottom';
 onSwitchAccount?: () => void;
 onMemories?: () => void;
 onDeletedConversations?: () => void;
 onLogout?: () => void;
 /** Render-prop trigger: gọi `open` từ onClick của phần tử. */
 children: (open: (e: MouseEvent<HTMLElement>) => void) => ReactNode;
}

export function AccountMenu({ placement = 'bottom', onSwitchAccount, onMemories, onDeletedConversations, onLogout, children }: AccountMenuProps) {
 const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

 const items: MenuItem[] = [
   { label: 'Chuyển đổi tài khoản', icon: <Users size={15} />, onClick: () => onSwitchAccount?.() },
   { label: 'Bộ nhớ của tôi', icon: <Brain size={15} />, onClick: () => onMemories?.() },
   { label: 'Đã xoá gần đây', icon: <History size={15} />, onClick: () => onDeletedConversations?.() },
   { label: 'Đăng xuất', icon: <LogOut size={15} />, danger: true, onClick: () => onLogout?.() },
 ];

 const open = (e: MouseEvent<HTMLElement>) => {
   const r = e.currentTarget.getBoundingClientRect();
   const width = 200;
   const menuHeight = items.length * 40 + 12;
   const x = Math.min(r.left, window.innerWidth - width - 8);
   const y = placement === 'top' ? Math.max(8, r.top - menuHeight - 6) : r.bottom + 6;
   setPos({ x, y });
 };

 return (
   <>
     {children(open)}
     <Menu
       open={pos !== null}
       x={pos?.x ?? 0}
       y={pos?.y ?? 0}
       items={items}
       onClose={() => setPos(null)}
     />
   </>
 );
}
