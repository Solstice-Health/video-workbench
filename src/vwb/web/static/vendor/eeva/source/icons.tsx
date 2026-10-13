import {
	ArrowLeft,
	ArrowUp,
	Bold,
	Calendar,
	Check,
	ChevronDown,
	ChevronLeft,
	ChevronRight,
	CircleAlert,
	FilePlus,
	FileText,
	Folder,
	Info,
	Italic,
	Library,
	Link,
	List,
	ListOrdered,
	LoaderCircle,
	type LucideIcon,
	Mail,
	Mic,
	Minus,
	PanelRightClose,
	PenLine,
	Plus,
	RefreshCw,
	Search,
	Sparkles,
	Square,
	Table,
	Trash2,
	TriangleAlert,
	Upload,
	X
} from 'lucide-react';

// Eeva V1 board 2 (Oct 6): every glyph the V1 final flow draws, keyed by the
// board's names (the flask is retired). The Solstice mark is the 26th; it is
// filled, so it lives in SolsticeMark rather than here.
export const ICONS = {
	'arrow-left': ArrowLeft,
	x: X,
	check: Check,
	'chevron-down': ChevronDown,
	'chevron-right': ChevronRight,
	'chevron-left': ChevronLeft,
	// The brand library's Hide the preview (J3, E7): the preview panel closing to the right.
	'panel-right-close': PanelRightClose,
	plus: Plus,
	// The attachment viewer's Zoom out, beside plus as Zoom in (#1053).
	minus: Minus,
	'arrow-up': ArrowUp,
	upload: Upload,
	file: FilePlus,
	// Not on board 2, but drawn in the flows: the panel header (A1) and library rows (J2, J3).
	'file-text': FileText,
	link: Link,
	search: Search,
	pen: PenLine,
	info: Info,
	'circle-alert': CircleAlert,
	// Error bars (E1).
	'triangle-alert': TriangleAlert,
	square: Square,
	refresh: RefreshCw,
	folder: Folder,
	// Tracking details' optional dates (V1.1 board 6 to 9).
	calendar: Calendar,
	// Select from brand library, in the AttachMenu (M1).
	library: Library,
	// The SuggestionChip's mark, in text.accent (board 3).
	sparkles: Sparkles,
	// The hero's Paste an email starter (U22).
	mail: Mail,
	// The composer's dictation: the mic, and its spinner while the last words transcribe.
	mic: Mic,
	loader: LoaderCircle,
	// Takes a queued message back (#1053).
	trash: Trash2,
	bold: Bold,
	italic: Italic,
	list: List,
	'list-ordered': ListOrdered,
	table: Table
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof ICONS;

export interface IconProps {
	name: IconName;
	/** Sizes in use on board 2: 12 (checks in labels and chips), 13 (checks in the thinking steps), 14 (buttons, bars, toolbar), 15 (AttachMenu items, M1), 16 (header, composer), 18 (attach, folder, Drop to attach). */
	size?: 12 | 13 | 14 | 15 | 16 | 18;
	className?: string;
}

/**
 * One of the registered icons (Eeva V1 board 2): Lucide outline, 2px stroke,
 * round caps and joins, in the text colour of its control.
 *
 * @status   draft
 * @variants size: 12 | 13 | 14 | 15 | 16 | 18
 * @use      Any icon in a design-system component or screen.
 * @avoid    An icon that is not registered here needs design sign-off first.
 */
export function Icon({ name, size = 16, className }: IconProps) {
	const Glyph = ICONS[name];
	return <Glyph aria-hidden='true' className={className} size={size} strokeWidth={2} />;
}
