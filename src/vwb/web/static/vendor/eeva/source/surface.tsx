import { cn } from '@/lib/utils';

export type SurfaceBackground = 'canvas' | 'chat' | 'none';

export interface SurfaceProps {
	children: React.ReactNode;
	className?: string;
	/** Switches every `--ds-*` token inside to that theme. Unset inherits. */
	theme?: 'editorial' | 'product';
	/** `canvas`: platform pages and the hero, with the ember wash on top. `chat`: the chat. `none`: the host page shows through. */
	background?: SurfaceBackground;
}

const BACKGROUND_CLASS: Record<SurfaceBackground, string> = {
	canvas: 'bg-[var(--ds-bg-canvas)] bg-[image:var(--ds-bg-wash)] bg-top bg-no-repeat [background-size:100%_var(--ds-bg-wash-height)]',
	chat: 'bg-[var(--ds-bg-chat)]',
	none: ''
};

/**
 * Theme scope and page background for a design-system screen. Tokens live on
 * `:root`, so components work without it; use it to switch themes on a
 * subtree or to paint a full page.
 *
 * @status   draft
 * @variants background: canvas | chat | none; theme: editorial | product
 * @use      Once around a full-page surface built from design-system components.
 * @avoid    Not around a single component. Not nested for layout.
 */
export function Surface({ children, className, theme, background = 'canvas' }: SurfaceProps) {
	return (
		<div
			className={cn('font-[family-name:var(--ds-font-ui)] text-[var(--ds-text-primary)]', BACKGROUND_CLASS[background], className)}
			data-ds-theme={theme}
		>
			{children}
		</div>
	);
}
