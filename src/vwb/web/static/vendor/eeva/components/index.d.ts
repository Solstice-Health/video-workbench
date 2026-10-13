/** Eeva components. Generated from design-system/ in Solstice-Health/Solstice-Frontend; documentation, not type-checked. */
import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import * as TabsPrimitive from '@radix-ui/react-tabs';

export interface AgentTableColumn {
    label: string;
    /** A grid track: "60px", "110px". Defaults to 1fr. */
    width?: string;
    /** Tertiary text, for a descriptor column like Visual (I1). */
    muted?: boolean;
}
/** Text, items and cells may carry **bold**, [label](url) and bare http(s) addresses, rendered as #1053 did. */
export type AgentMessageBlock = {
    type: 'text';
    text: string;
} | {
    type: 'bullets';
    items: string[];
} | {
    type: 'numbered';
    items: string[];
} | {
    type: 'table';
    columns: AgentTableColumn[];
    rows: string[][];
};
export interface AgentMessageProps {
    /** Paragraphs, bullet lists and tables, in order. */
    blocks?: AgentMessageBlock[];
    /** Shorthand for a single paragraph. */
    children?: React.ReactNode;
    /** Buttons 10px under the message, like Try again and Finish with what we have after a failure (G2). */
    actions?: React.ReactNode;
    /** "4:24 PM", top right, shown only on hover or focus (as #1053). */
    time?: string;
    className?: string;
}
/**
 * The agent's words (Eeva V1 board 4, from A2, C2, I1): plain 15/24 in
 * primary text, no bubble, no avatar. Bullets sit 4px under their lead-in
 * at a 20px indent; a table is a small-caps header over hairline rows.
 * Bold, links and bare addresses render inline; the time shows on hover.
 *
 * @status   draft
 * @variants plain; bullets; numbered; table; actions; inline bold and links; hover time
 * @use      Every agent reply in the feed, built from blocks (`toAgentBlocks` reads the agent's Markdown into them).
 * @avoid    Never a bubble. A table here is for reading; it is never stored as a table in the panel.
 */
export declare function AgentMessage({ blocks, children, actions, time, className }: AgentMessageProps): import("react").JSX.Element;
export interface AttachMenuOption {
    label: string;
    icon: IconName;
    onSelect: () => void;
}
export interface AttachMenuProps {
    /** Upload files, then Select from brand library (M1). */
    options: AttachMenuOption[];
    /** The trigger: the composer's + or the panel's Add files. */
    children: React.ReactElement;
    /** Above the composer's + (top, start); below the panel's Add files (bottom, end). */
    side?: 'top' | 'bottom';
    align?: 'start' | 'end';
    /** Nudges the menu along the trigger, e.g. -6 to line up with the composer's text (M1). */
    alignOffset?: number;
    /** A hover label on the trigger, like Attach files on the composer's +. */
    tooltip?: string;
}
/**
 * Where a file comes from (Eeva V1 board 3, from M1 and M2): the same
 * dropdown behind the composer's + and the panel's Add files. 232px, radius 10,
 * 36px items with a 15px icon; the hovered item takes an ink 5% fill.
 *
 * @status   draft
 * @use      Wrap the + or Add files button; the items open the file picker or the brand library.
 * @avoid    Not a general menu: two or three attachment sources only.
 */
export declare function AttachMenu({ options, children, side, align, alignOffset, tooltip }: AttachMenuProps): import("react").JSX.Element;
export type AttachmentFileType = 'pdf' | 'img' | 'doc';
export type AttachmentStatus = 'ready' | 'uploading' | 'failed';
export interface AttachmentThumbProps {
    name: string;
    fileType: AttachmentFileType;
    status?: AttachmentStatus;
    /** 0 to 100 while uploading: the 2px bar along the bottom. Unset while the server reads the file. */
    progress?: number;
    /** Failed: why, as the thumb's hover title (no meta line; the boards draw none). */
    failure?: string;
    /** Staged in the composer: the × at the end (M5, M6). Its tooltip reads Remove file. */
    onRemove?: () => void;
    /** The badge and name open the file (a viewer); the × stays separate. */
    onOpen?: () => void;
    className?: string;
}
/**
 * One attached file (Eeva V1 board 4, from A1, M5, M6): a file-type badge and
 * the name on white, 32px. Above a sent message it carries a soft edge; staged
 * in the composer it takes a firmer edge, an ×, and while uploading a 2px
 * progress bar along the bottom.
 *
 * @status   draft
 * @variants status: ready | uploading | failed; removable (staged); openable
 * @use      Above a sent message in `UserMessage`, and staged in the Composer's `attachments` slot with `onRemove`. `onOpen` to preview it.
 * @avoid    Not a link or a tag. A file in the request panel is a reference-file row.
 */
export declare function AttachmentThumb({ name, fileType, status, progress, failure, onRemove, onOpen, className }: AttachmentThumbProps): import("react").JSX.Element;
export type ButtonVariant = 'primary' | 'secondary' | 'surface' | 'destructive' | 'text';
export type ButtonSize = 'sm' | 'md';
export interface ButtonProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'type'> {
    variant?: ButtonVariant;
    size?: ButtonSize;
    /** A 32px circle at any size (board 3: Attach, Send, Stop, Close). Requires `aria-label`; pair it with a `Tooltip`. */
    iconOnly?: boolean;
    /** Keeps the label, adds the animated dots and blocks clicks until it resolves. */
    loading?: boolean;
    /** Always a real button; `submit` only inside a form. */
    type?: 'button' | 'submit';
}
/**
 * The one button for every surface (Eeva V1 board 3, Oct 5).
 *
 * @status   draft
 * @variants variant: primary | secondary | surface | destructive | text; size: sm | md; iconOnly; disabled; loading; pressed
 * @use      `primary`: one per view, the main action (Submit request, Send). `secondary`: everything else, and the panel header's waiting state (Preparing or Updating request) with `loading`, 60% and animated dots (board 3). `surface`: the composer's Stop. `destructive`: an action that loses work (Change and start over). `text`: low emphasis (Skip, Finish with what we have, Attach).
 * @avoid    Navigation inside a sentence is `TextLink`. Never two `primary` buttons in one view.
 */
export declare const Button: import("react").ForwardRefExoticComponent<ButtonProps & import("react").RefAttributes<HTMLButtonElement>>;
export type CheckboxProps = Omit<React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root>, 'asChild'>;
/**
 * A multiple-choice box (Eeva V1 board 3b). 18px, radius 4; a 1.5px ring at
 * rest that darkens on hover; checked is ink with a white check.
 *
 * @status   draft
 * @variants checked; disabled
 * @use      Choosing several of a set, like banner sizes or library items. Name it with a `<label>` or `aria-label`.
 * @avoid    A single on/off setting is not a checkbox here. One choice of several is `SelectField`.
 */
export declare const Checkbox: import("react").ForwardRefExoticComponent<CheckboxProps & import("react").RefAttributes<HTMLButtonElement>>;
export interface ChecklistRowProps extends Omit<CheckboxProps, 'children' | 'className' | 'checked' | 'onCheckedChange'> {
    label: string;
    /** One line under the label. */
    description?: string;
    checked: boolean;
    onCheckedChange: (checked: boolean) => void;
    className?: string;
}
/**
 * One choice in a multi-select list (Eeva V1 board 3b, from F1): Checkbox,
 * label and subtext. A checked row takes the sunken fill; the whole row is
 * the click target.
 *
 * @status   draft
 * @variants checked; disabled
 * @use      A stack of rows 2px apart, like the multi-select `ComposerQuestion` (Which platforms?).
 * @avoid    One choice of several is the numbered option rows. A lone checkbox is `Checkbox`.
 */
export declare const ChecklistRow: import("react").ForwardRefExoticComponent<ChecklistRowProps & import("react").RefAttributes<HTMLButtonElement>>;
/** The mic. The caller records (`useDictation` in the intake); the box is read-only while it runs. */
interface ComposerDictation {
    state: 'idle' | 'recording' | 'transcribing';
    /** While recording, shown on the stop pill as 0:12. */
    elapsedMs: number;
    onStart: () => void;
    onStop: () => void;
}
export interface ComposerProps {
    value: string;
    onValueChange: (value: string) => void;
    /** Enter or Send, with the trimmed text. While the agent works the caller queues it. */
    onSend: (value: string) => void;
    /** False while nothing can go yet (files still reading or failed): Send disables and Enter does nothing. */
    canSend?: boolean;
    /** Shows the Attach button. */
    onAttach?: () => void;
    /** Attach opens the AttachMenu with these sources (Upload files, Select from brand library) instead of calling `onAttach`. */
    attachOptions?: AttachMenuOption[];
    /** Shows the mic, left of Send. */
    dictation?: ComposerDictation;
    /** The text box's paste, so the caller can turn a long one into an attachment. */
    onPaste?: React.ClipboardEventHandler<HTMLTextAreaElement>;
    /** With `working`, shows Stop 6px left of Send. */
    onStop?: () => void;
    /** The agent is working: Send stays and queues, Stop appears. */
    working?: boolean;
    placeholder?: string;
    /** The text box's accessible name. */
    label?: string;
    /** Staged `AttachmentThumb`s (with `onRemove`), wrapping in a row above the text (M5, M6). */
    attachments?: React.ReactNode;
    /** A file is being dragged over the page: the box shows Drop to attach (M7). The caller handles the drop. */
    dragging?: boolean;
    /** A docked `ComposerQuestion`, above a hairline; the text box below becomes its typed answer. */
    question?: React.ReactNode;
    /** Two to four `SuggestionChip`s, 8px above the composer. */
    suggestions?: React.ReactNode;
    /** A `SuggestionChip variant='generate'`, right-aligned 8px above the composer. */
    generateChip?: React.ReactNode;
    /** Pinned to the foot of the feed: adds the backing that blurs and fades the feed behind it. */
    docked?: boolean;
    className?: string;
}
/**
 * Where the person writes (Eeva V1 board 4, from A5 and B1). White, radius
 * 16, the composer lift and no grey border; it lifts further on focus. At
 * rest: placeholder, Attach and Send. While the agent works Send stays and
 * queues, and a round Stop appears beside it.
 *
 * @status   draft
 * @variants at rest; working; with files; with a question; with chips; docked; attach menu; dictation; dragging
 * @use      Once, at the foot of the chat. Questions, chips and files go in its slots.
 * @avoid    Not a form field: one-line answers in the panel are `TextField`. Never two composers in a view.
 */
export declare const Composer: import("react").ForwardRefExoticComponent<ComposerProps & import("react").RefAttributes<HTMLTextAreaElement>>;
/** `2026-10-16` -> `Oct 16, 2026`. Anything unparseable comes back as is. */
export declare function formatDateValue(iso: string): string;
export interface DateFieldProps extends Pick<React.InputHTMLAttributes<HTMLInputElement>, 'id' | 'name' | 'required' | 'aria-label' | 'aria-describedby'> {
    /** ISO date, `YYYY-MM-DD`. Empty string for no date. */
    value?: string;
    defaultValue?: string;
    onValueChange?: (value: string) => void;
    error?: boolean;
    placeholder?: string;
    className?: string;
}
/**
 * A date (Eeva V1.1, boards 6 to 9: Tracking details' optional dates). The
 * field shell with a calendar icon and mm/dd/yyyy; reads as `Oct 16, 2026` at
 * rest and becomes the browser's native date entry and picker while editing.
 *
 * @status   draft
 * @variants error
 * @use      PRC submission, AfD target and Live date, under Dates · optional in Tracking details.
 * @avoid    Not for a date range or a time.
 */
export declare const DateField: import("react").ForwardRefExoticComponent<DateFieldProps & import("react").RefAttributes<HTMLInputElement>>;
export type FieldReadiness = 'filled' | 'missing' | 'none';
export interface FieldLabelProps {
    children: React.ReactNode;
    htmlFor?: string;
    required?: boolean;
    /** Required fields only: a check when filled, a dotted ember circle when missing. */
    readiness?: FieldReadiness;
    className?: string;
}
/**
 * A request field's label (Eeva V1 board 3b, from A1 and A5): 13px secondary
 * text, the red asterisk when required, and a readiness mark before the text.
 *
 * @status   draft
 * @variants required; readiness: filled | missing | none
 * @use      Every row of the request panel. The panel's meta line carries the count until the request is ready.
 * @avoid    Optional fields never get a mark. Not a badge or a dot: the check and the dotted circle are the only readiness signals.
 */
export declare function FieldLabel({ children, htmlFor, required, readiness, className }: FieldLabelProps): import("react").JSX.Element;
/**
 * Marks a required field: a red asterisk right after the label. Optional
 * fields get no marker at all; there is no other required/optional signal
 * anywhere on the row.
 *
 * @status   draft
 * @use      Right after the label of any required field.
 * @avoid    Not a badge and not a dot. Required is the asterisk, nothing else.
 */
export declare function FieldMarker(): import("react").JSX.Element;
export declare function FieldShell({ children, error, tall, className }: {
    children: React.ReactNode;
    error?: boolean;
    /** 40px instead of 32px. Search only. */
    tall?: boolean;
    className?: string;
}): import("react").JSX.Element;
export declare const FIELD_INPUT_CLASS = "h-full min-w-0 flex-1 truncate bg-transparent text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-primary)] outline-none placeholder:text-[var(--ds-text-tertiary)]";
export declare const FIELD_ICON_CLASS = "shrink-0 text-[var(--ds-text-tertiary)]";
export type FieldValueKind = 'text' | 'textarea' | 'select' | 'checklist' | 'url';
export type FieldValueState = 'idle' | 'loading' | 'updated' | 'missing' | 'stopped';
/** `checklist` only: a typed choice after the list, e.g. Custom size…. */
export interface FieldValueCustom {
    label: string;
    /** The stored form of the typed text, or null when it is not a valid choice. */
    normalize: (text: string) => string | null;
}
export interface FieldValueProps {
    kind?: FieldValueKind;
    state?: FieldValueState;
    /** `checklist` values are comma-joined. */
    value: string;
    onValueChange?: (value: string) => void;
    /** `select` and `checklist`. */
    options?: SelectOption[];
    custom?: FieldValueCustom;
    placeholder?: string;
    id?: string;
    'aria-label'?: string;
    'aria-describedby'?: string;
    className?: string;
}
/**
 * The value slot of a request panel row in one of its states (Eeva V1 board
 * 3b): the agent loading it, the agent having updated it, required and
 * missing, or left empty by a Stop. Renders the right field for `kind`.
 *
 * @status   draft
 * @variants kind: text | textarea | select | checklist | url; state: idle | loading | updated | missing | stopped
 * @use      Every row of RequestDocument. `loading` while the agent writes the value (A1, C1); `updated` after it rewrote one (C2), cleared by the first edit; `missing` once a required question is skipped (E1); `stopped` for a value the agent never reached (B3). `checklist` for several choices (Banner sizes, Platform), with `custom` for a typed one.
 * @avoid    Not for Notes (`RichTextArea`) or a file's note. No hint text under the field: the panel's MessageBar says what is wrong.
 */
export declare function FieldValue({ kind, state, value, onValueChange, options, custom, placeholder, id, className, ...aria }: FieldValueProps): import("react").JSX.Element;
export type MessageBarTone = 'progress' | 'info' | 'success' | 'error';
export interface MessageBarProps {
    tone: MessageBarTone;
    children: React.ReactNode;
    /** Shows the ×. `info` and `success` only: progress and error stay until their state ends. */
    onDismiss?: () => void;
    className?: string;
}
/**
 * One line of status at the top of the request panel body (Eeva V1 board 5,
 * from C1, C2, E1, B3, G3). Sand band, 13/18 text, a 14px icon aligned to the
 * first line; info and success bars carry an ×. Success is sage: the request
 * is ready to review (A5).
 *
 * @status   draft
 * @variants tone: progress | info | success | error; onDismiss (info, success)
 * @use      `progress` while the agent prepares or updates (Preparing your request…, Updating Audience and Topic…). `info` for what just happened (Updated, Stopped, Set up without reading a file). `success` once, when the request first becomes ready (Ready to review). `error` for a blocking gap (1 required field is still empty).
 * @avoid    One bar at a time. Never in the chat, never a toast. An error about one field also rings that field.
 */
export declare function MessageBar({ tone, children, onDismiss, className }: MessageBarProps): import("react").JSX.Element;
export interface ModalFrameProps extends Omit<React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>, 'asChild'> {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    /** Blurs the page 2px under the scrim, as the brand library does (J1). A confirm dims only (D1). */
    blur?: boolean;
}
/**
 * The scrim and lifted panel every Eeva modal sits in (Eeva V1 board 7): the
 * ink 26% scrim (plus a 2px blur for the brand library), elevation.modal, rising 8px on the way in.
 * Size, radius and layout come from `className`; the panel needs a `DialogPrimitive.Title`.
 *
 * @status   draft
 * @variants none: size it with `className`
 * @use      The base of `Modal` and `LibraryModal`, and of any new modal.
 * @avoid    Use `Modal` for a confirm. Not for a popover or a drawer.
 */
export declare function ModalFrame({ open, onOpenChange, blur, className, children, ...props }: ModalFrameProps): import("react").JSX.Element;
export interface ModalProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    title: string;
    /** The message under the title. */
    children: React.ReactNode;
    /** The safe action, first in the footer. It takes focus and closes. */
    cancelLabel: string;
    confirmLabel: string;
    /** `destructive` when confirming loses work. */
    tone?: 'primary' | 'destructive';
    onConfirm: () => void;
}
/**
 * Eeva's dialog (Eeva V1 board 7, from frame D1): 400px, radius 14, a 17px
 * title, a 14/21 message, and the safe action beside a primary or
 * destructive one. Focus starts on the safe action, so Enter never confirms
 * by accident; Escape and the scrim cancel.
 *
 * @status   draft
 * @variants tone: primary | destructive
 * @use      A question that needs an answer before going on: the major-change confirm (Change and start over).
 * @avoid    Not for picking things; that is `LibraryModal`. Not for news the user does not have to answer.
 */
export declare function Modal({ open, onOpenChange, title, children, cancelLabel, confirmLabel, tone, onConfirm }: ModalProps): import("react").JSX.Element;
/** Markdown in the toolbar's subset as HTML for the editor. Anything else stays as text. */
export declare function markdownToHtml(markdown: string): string;
/** The editor's content as Markdown, the inverse of `markdownToHtml`. */
export declare function htmlToMarkdown(root: HTMLElement): string;
/** The first table in pasted HTML, rebuilt from its cell text; null when there is none. */
export declare function pastedTableHtml(html: string): string | null;
export interface RichTextAreaProps {
    /** Markdown. */
    value: string;
    /** Markdown, on every edit. */
    onChange?: (markdown: string) => void;
    /** When focus leaves the field and its toolbar: a natural moment to save. */
    onBlur?: () => void;
    placeholder?: string;
    /** A Skeleton inside the field while the agent writes the notes (A1). */
    loading?: boolean;
    autoFocus?: boolean;
    id?: string;
    'aria-label'?: string;
    className?: string;
}
/**
 * The request's Notes (Eeva V1 board 3b, from I4). Edited formatted, stored as
 * Markdown; a toolbar with bold, italic, bulleted and numbered lists, link and
 * table appears on focus. Grows with its content.
 *
 * @status   draft
 * @variants loading; empty; filled; focused (toolbar)
 * @use      The Notes section of the request panel: anything the team should know that has no field.
 * @avoid    Not for a short value or a file's note (`TextField`). No headings, images or colours: only what the toolbar writes.
 */
export declare function RichTextArea({ loading, className, ...props }: RichTextAreaProps): import("react").JSX.Element;
export type SearchFieldProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, 'disabled' | 'type'>;
/**
 * Filters a list in place (Eeva V1 board 3b): the field shell at 40px with a
 * leading search icon.
 *
 * @status   draft
 * @use      The search above a library tab or a long list.
 * @avoid    It has no error state: an empty result is said in the list, not on the field.
 */
export declare const SearchField: import("react").ForwardRefExoticComponent<SearchFieldProps & import("react").RefAttributes<HTMLInputElement>>;
export interface SelectOption {
    value: string;
    label: string;
}
export declare const SELECT_TRIGGER_CLASS = "flex h-[var(--ds-size-sm)] w-full items-center gap-2 rounded-[var(--ds-radius-md)] bg-[var(--ds-surface-default)] px-2.5 text-left text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-primary)] outline-none transition-shadow [transition-duration:var(--ds-dur-fast)] data-[placeholder]:text-[var(--ds-text-tertiary)]";
export declare const SELECT_MENU_CLASS = "z-[var(--ds-z-dropdown)] min-w-[var(--radix-select-trigger-width)] animate-ds-popover-in overflow-hidden rounded-[var(--ds-radius-lg)] bg-[var(--ds-surface-default)] p-1 font-[family-name:var(--ds-font-ui)] [box-shadow:var(--ds-elevation-panel)]";
export interface SelectFieldProps {
    options: SelectOption[];
    value?: string;
    defaultValue?: string;
    onValueChange?: (value: string) => void;
    placeholder?: string;
    /** Drawn inside the trigger before the value, for a compact count select: "Subject lines 3 ⌄" (E5, Copy's How many row). Pass `className='w-auto'`. Also the default aria-label. */
    label?: string;
    error?: boolean;
    id?: string;
    name?: string;
    'aria-label'?: string;
    'aria-describedby'?: string;
    className?: string;
}
/**
 * One choice of a few (Eeva V1 board 3b): the field shell with a trailing
 * chevron. Board 3b draws the closed field only; the open menu here is the
 * smallest one built from existing tokens until it is drawn.
 *
 * @status   draft
 * @variants error; label (compact count select)
 * @use      Priority, channel, format: a short closed list. With `label`, an auto-width count like Subject lines 3 in Copy's How many row (E5).
 * @avoid    Several choices at once are Checkboxes. Never for navigation.
 */
export declare function SelectField({ options, placeholder, label, error, className, ...props }: SelectFieldProps): import("react").JSX.Element;
export type SkeletonShape = 'field' | 'notes';
export interface SkeletonProps {
    shape?: SkeletonShape;
    className?: string;
}
/**
 * A placeholder bar while a value loads (Eeva V1 boards 5 and 1c). Ink 6%
 * with a 90% white band sweeping every 3.2s; the band is fixed to the
 * viewport, so every bar on screen shares one wave.
 *
 * @status   draft
 * @variants shape: field | notes
 * @use      In place of a field value while the agent prepares or updates it (A1, C1), and inside Notes. The label stays.
 * @avoid    Never in place of a label or a heading. Never gray blocks for a whole layout: only the values that are on their way.
 */
export declare function Skeleton({ shape, className }: SkeletonProps): import("react").JSX.Element;
export interface SolsticeMarkProps {
    /** 16 in the ThinkingBlock. */
    size?: number;
    /** Turns slowly and evenly in its own plane while the agent streams (motion.spin); still when done. */
    spinning?: boolean;
    className?: string;
}
/**
 * The Solstice mark (Eeva V1 board 2), taken from the full logo: filled, in
 * the current text colour. The ThinkingBlock shows it in the accent and
 * turning while streaming, in ink and still when done.
 *
 * @status   draft
 * @variants spinning
 * @use      The agent's presence in the ThinkingBlock.
 * @avoid    Not a logo lockup; that is the brand asset, not an icon.
 */
export declare function SolsticeMark({ size, spinning, className }: SolsticeMarkProps): import("react").JSX.Element;
export type SuggestionChipVariant = 'suggestion' | 'generate' | 'starter';
export interface SuggestionChipProps {
    /** The suggestion, sent as the message when clicked. */
    text: string;
    onSelect: (text: string) => void;
    /** `generate`: the pill that skips the optional questions (A6). `starter`: a hero Starter (U22). */
    variant?: SuggestionChipVariant;
    /** The leading glyph. `generate` defaults to the pen; `suggestion` has none (V1.1 option F). */
    icon?: IconName;
    /** `starter`: chosen, while it steers the composer (its placeholder). */
    pressed?: boolean;
    disabled?: boolean;
    className?: string;
}
/**
 * A chip above the composer (Eeva V1 board 3). `suggestion` (A7, V1.1 option F):
 * a next message, 30px, 13/500, white at 80% inside a 1px ember ring that
 * deepens on hover; pressed is ink. `generate` (A6): Skip questions, generate
 * my brief. `starter`: the hero Starters (U22).
 *
 * @status   draft
 * @variants variant: suggestion | generate | starter; pressed; disabled
 * @use      `suggestion`: two to four in the Composer's `suggestions` slot, a fixed row from turn 1, refreshed each turn, Draft copy first on copy-bearing types (N1, N2); clicking sends the text. `generate`: the Composer's `generateChip` slot, right-aligned, while optional questions remain.
 * @avoid    Not while the agent is working. A docked question keeps the row (N1). Never `generate` on a required question.
 */
export declare function SuggestionChip({ text, onSelect, variant, icon, pressed, disabled, className }: SuggestionChipProps): import("react").JSX.Element;
export declare const Tabs: import("react").ForwardRefExoticComponent<TabsPrimitive.TabsProps & import("react").RefAttributes<HTMLDivElement>>;
export declare const TabPanel: import("react").ForwardRefExoticComponent<TabsPrimitive.TabsContentProps & import("react").RefAttributes<HTMLDivElement>>;
export declare function TabList({ children, className, ...props }: React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>): import("react").JSX.Element;
export interface TabProps extends Omit<React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>, 'children'> {
    children: React.ReactNode;
    /** A count badge after the label. Hidden at 0. */
    count?: number;
}
/**
 * Underlined tabs with an optional count (Eeva V1 board 3). 40px, 24px
 * apart, 14px; the active tab is 600 with a 2px ink underline, the others
 * 500 in secondary text. The count badge is ink on the active tab.
 *
 * @status   draft
 * @variants count
 * @use      Switching views of one thing: Request Detail's Details and Files, the brand library's sources.
 * @avoid    Not for navigation between pages.
 */
export declare function Tab({ children, count, className, ...props }: TabProps): import("react").JSX.Element;
export interface TextAreaProps extends Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, 'disabled' | 'rows'> {
    /** 2 for Goal, 4 for Notes (board 3b). */
    rows?: 2 | 4;
    error?: boolean;
}
/**
 * Multi-line text entry (Eeva V1 board 3b). The same rings as `TextField`,
 * 8px top and bottom inset, sized by its visible lines.
 *
 * @status   draft
 * @variants rows: 2 | 4; error
 * @use      Goal (2 lines), Notes (4 lines), a note on a file.
 * @avoid    Not for a single short value; that is `TextField`.
 */
export declare const TextArea: import("react").ForwardRefExoticComponent<TextAreaProps & import("react").RefAttributes<HTMLTextAreaElement>>;
export declare const FIELD_BOX_CLASS = "rounded-[var(--ds-radius-md)] bg-[var(--ds-surface-default)] px-2.5 text-[length:var(--ds-type-ui-size)] leading-[var(--ds-type-ui-leading)] text-[var(--ds-text-primary)] outline-none placeholder:text-[var(--ds-text-tertiary)] transition-shadow [transition-duration:var(--ds-dur-fast)] [transition-timing-function:var(--ds-ease-standard)]";
export declare function fieldRingClass(error: boolean): "[box-shadow:var(--ds-field-error)]" | "[box-shadow:var(--ds-field-rest)] hover:[box-shadow:var(--ds-field-hover)] focus:[box-shadow:var(--ds-field-editing)] focus-within:[box-shadow:var(--ds-field-editing)]";
export interface TextFieldProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'disabled'> {
    error?: boolean;
}
/**
 * Single-line text entry (Eeva V1 board 3b). 32px, radius 8, white with a
 * 12% ring at rest, 20% on hover, an ink ring and halo while editing.
 *
 * @status   draft
 * @variants error
 * @use      A short string in a row or small form: Requester, Topic, Goal.
 * @avoid    No disabled state: a read-only value renders as plain text. A link is `UrlField`.
 */
export declare const TextField: import("react").ForwardRefExoticComponent<TextFieldProps & import("react").RefAttributes<HTMLInputElement>>;
export type TextLinkKind = 'inline' | 'url' | 'back';
export interface TextLinkProps {
    href: string;
    kind?: TextLinkKind;
    /** Opens in a new tab. */
    external?: boolean;
    className?: string;
    children: React.ReactNode;
}
/**
 * A link (Eeva V1 board 3). Links navigate; buttons do things. Board 3 draws
 * rest only; hover is the smallest change that reads as a link.
 *
 * @status   draft
 * @variants kind: inline | url | back; external
 * @use      `inline`: an action inside a sentence, like View full chat history. `url`: a web address shown as itself. `back`: View all requests, above a page title.
 * @avoid    Never for an action that does not navigate. A standalone Exit is a `text` Button.
 */
export declare function TextLink({ href, kind, external, className, children }: TextLinkProps): import("react").JSX.Element;
export type ThinkingState = 'streaming' | 'done' | 'stopped' | 'failed';
/** One step: its label, a list under it if any, and how it ended. */
export interface ThinkingBlockStep {
    label: string;
    /** One line each under the label: "Reading 3 claims" and the three claims (#1053). */
    items?: string[];
    /** `failed`: the step the turn failed on; the list opens to show it. Unset reads done within `completed`. */
    status?: 'done' | 'failed';
}
export interface ThinkingBlockProps {
    state: ThinkingState;
    /** Streaming: the live thought, replaced as the next one arrives. */
    thought?: string;
    /** Done: what the turn did, "Read your message". */
    summary?: string;
    /** Every step of the turn, in order: a label, or a step with its list and status. */
    steps?: (string | ThinkingBlockStep)[];
    /** Steps finished so far. Defaults to all of them once done. */
    completed?: number;
    /** Whole seconds the turn has run. */
    seconds?: number;
    /** Failed: how many tries ran, "3 tries · failed" (G2). */
    attempts?: number;
    /** Controlled open state of the steps list. */
    expanded?: boolean;
    defaultExpanded?: boolean;
    onExpandedChange?: (expanded: boolean) => void;
    className?: string;
}
/**
 * The agent at work, then out of the way (Eeva V1 board 4, from A1, A2, B3,
 * G2). Streaming: the Solstice mark turns slowly in the accent beside the
 * thought (15/24 500, no shimmer), the steps collapsed beneath. Done, stopped or failed: one quiet
 * row, the mark in ink and still; click it to open the steps. A step can carry
 * a list (the claims being read); a failed step opens the list so it can't be
 * missed (#1053).
 *
 * @status   draft
 * @variants state: streaming | done | stopped | failed; expanded; step lists; a failed step
 * @use      Above the agent's reply in a turn. In the feed, leave 12px under a collapsed row (A2 pulls it 10px into the 22px feed gap).
 * @avoid    Not a spinner for other loading. The agent's words after a stop or failure go in an `AgentMessage`, with actions after a failure.
 */
export declare function ThinkingBlock({ state, thought, summary, steps: stepsProp, completed: completedProp, seconds, attempts, expanded, defaultExpanded, onExpandedChange, className }: ThinkingBlockProps): import("react").JSX.Element;
export interface TooltipProps {
    /** The label, or a rich tooltip's body (up to three lines). */
    content: string;
    /** Makes it a rich tooltip: a title above the body, up to 320px wide. */
    title?: string;
    /** A real keyboard shortcut, shown as a chip after the label. */
    shortcut?: string;
    side?: 'top' | 'bottom' | 'left' | 'right';
    children: React.ReactNode;
    /** Controlled visibility, for previews and tests. Leave unset for hover and focus. */
    open?: boolean;
}
/**
 * A hover and focus tooltip (Eeva V1 board 3). Ink, 12/16 500, radius 6,
 * max 240px, 6px arrow, 8px from its anchor. Shows after 400ms hover or at
 * once on keyboard focus; hides on leave, blur or Escape; flips to stay in
 * view. The board's fade is 120ms; this uses motion.fast (150ms), the
 * nearest token.
 *
 * @status   draft
 * @variants label; shortcut; rich (title)
 * @use      Label every icon-only button. A rich tooltip explains why, like what a file was attached for.
 * @avoid    Buttons with visible text get none. Never interactive: no links or buttons inside; that is a popover.
 */
export declare function Tooltip({ content, title, shortcut, side, children, open }: TooltipProps): import("react").JSX.Element;
export interface UrlFieldProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'disabled' | 'type'> {
    error?: boolean;
}
/**
 * A web address (Eeva V1 board 3b): the field shell with a leading link
 * icon. The value reads in the accent text colour in every state; on error
 * the icon turns red too, with no text (Eeva V1: no inline errors).
 *
 * @status   draft
 * @variants error
 * @use      CTA link, any URL the request points at.
 * @avoid    Not for free text. Validation belongs to the caller; this only shows the result.
 */
export declare const UrlField: import("react").ForwardRefExoticComponent<UrlFieldProps & import("react").RefAttributes<HTMLInputElement>>;
export interface UserMessageProps {
    children: React.ReactNode;
    /** "4:24 PM", beneath the bubble. Replaced by the Queued line while queued. */
    time?: string;
    /** `AttachmentThumb`s (sm), in a row above the bubble. */
    attachments?: React.ReactNode;
    /** Sent while the agent works: the bubble at 60% and the Queued line under it. */
    queued?: boolean;
    /** Queued only: a trash on the Queued line takes the message back. */
    onRemoveQueued?: () => void;
    className?: string;
}
/**
 * Your message (Eeva V1 board 4, from A1, C1, B1): a bubble on the right,
 * 15/24, at most 78% wide, radius 18, the time beneath and any files above,
 * 6px apart.
 *
 * @status   draft
 * @variants plain; with files; files only (no bubble); queued (removable)
 * @use      Everything the person sends, and the answer half of an `AnsweredPair`.
 * @avoid    No avatar, no name. The agent's words are `AgentMessage`.
 */
export declare function UserMessage({ children, time, attachments, queued, onRemoveQueued, className }: UserMessageProps): import("react").JSX.Element;
export declare const ICONS: {
    'arrow-left': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    x: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    check: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'chevron-down': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'chevron-right': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'chevron-left': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'panel-right-close': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    plus: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    minus: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'arrow-up': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    upload: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    file: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'file-text': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    link: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    search: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    pen: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    info: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'circle-alert': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'triangle-alert': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    square: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    refresh: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    folder: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    calendar: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    library: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    sparkles: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    mail: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    mic: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    loader: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    trash: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    bold: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    italic: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    list: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    'list-ordered': import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
    table: import("react").ForwardRefExoticComponent<Omit<import("lucide-react").LucideProps, "ref"> & import("react").RefAttributes<SVGSVGElement>>;
};
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
export declare function Icon({ name, size, className }: IconProps): import("react").JSX.Element;
export type SurfaceBackground = 'canvas' | 'chat' | 'none';
export interface SurfaceProps {
    children: React.ReactNode;
    className?: string;
    /** Switches every `--ds-*` token inside to that theme. Unset inherits. */
    theme?: 'editorial' | 'product';
    /** `canvas`: platform pages and the hero, with the ember wash on top. `chat`: the chat. `none`: the host page shows through. */
    background?: SurfaceBackground;
}
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
export declare function Surface({ children, className, theme, background }: SurfaceProps): import("react").JSX.Element;
