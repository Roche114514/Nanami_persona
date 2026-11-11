// Normalize transport chat ids to one stable key.
//
// The OneBot channel and the persona hook have received both "869687234" and
// "group:869687234" for the same room. Prefixing those blindly produced
// "private:group:869687234" and split one person into two state histories.

const CHAT_KINDS = new Set(['group', 'private']);

export function normalizeChatKey(value) {
    const raw = String(value ?? '').trim();
    if (!raw) {
        return '';
    }
    // A chat id may already carry one or more transport prefixes. The last
    // explicit private/group segment is the authoritative one.
    const parts = raw.split(':').filter(Boolean);
    for (let index = parts.length - 2; index >= 0; index -= 1) {
        const kind = parts[index].toLowerCase();
        if (CHAT_KINDS.has(kind) && parts[index + 1]) {
            return `${kind}:${parts.slice(index + 1).join(':')}`;
        }
    }
    return raw;
}

export function chatKeyOfContext(ctx) {
    const raw = String(ctx?.chatId ?? ctx?.peerId ?? '').trim();
    if (!raw) {
        return '';
    }
    const normalized = normalizeChatKey(raw);
    if (/^(?:group|private):/u.test(normalized)) {
        return normalized;
    }
    const kind = String(ctx?.chatType ?? ctx?.type ?? '').toLowerCase();
    const prefix = /group|room|channel/u.test(kind) ? 'group' : 'private';
    return `${prefix}:${normalized}`;
}
