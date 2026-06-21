
## サブレーン開発フロー (opt-in profile)

- 本 project は `scaffold apply <preset> --with-sublane-flow` でサブレーン開発フローを runtime-active な参照として有効化している。default scaffold では本節は生成されない。
- 配布された opt-in entrypoint doc `vibes/docs/profiles/sublane-flow-runtime-profile.md` を読み、そこから `mozyo-bridge-agent` skill workflow reference の sublane sections へ辿る。router 本文に workflow 詳細を複製しない。
- lane 数・cockpit 構成・絶対 path・session 命名などの private operating policy は本 profile に含まれない。adopter は自身の operating profile を別途定義する。
