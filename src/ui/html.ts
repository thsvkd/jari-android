// 화면 조각을 문자열로 만드는 데 쓰는 세 가지. 앱·달력·컴포넌트가 모두 이 escape 하나를 써요.

/** 믿고 그대로 넣는 HTML 조각. 글자(label, title, text…)는 이 타입이 아니라 string 으로 받아 escape 해요. */
export type Html = string;

type AttrValue = string | number | boolean | null | undefined;
export type Attrs = Record<string, AttrValue>;

const ENTITIES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ENTITIES[character] ?? character);
}

/**
 * 속성 목록. aria-*·data-* 의 참/거짓은 "true"/"false" 글자로 쓰고,
 * 그 밖의 참/거짓(disabled, checked, required…)은 붙이거나 빼요. null·undefined 는 빼요.
 */
export function attrs(values: Attrs): string {
  return Object.entries(values)
    .flatMap(([name, value]) => {
      if (value === null || value === undefined) return [];
      if (typeof value === "boolean") {
        if (name.startsWith("aria-") || name.startsWith("data-")) return [`${name}="${value}"`];
        return value ? [name] : [];
      }
      return [`${name}="${esc(value)}"`];
    })
    .join(" ");
}

/** 클래스 이름을 이어 붙여요. 빈 값은 건너뛰어 빈칸이 두 번 생기지 않아요. */
export function cx(...names: Array<string | false | null | undefined>): string {
  return names.filter(Boolean).join(" ");
}
