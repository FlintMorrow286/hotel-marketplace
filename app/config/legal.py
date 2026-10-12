"""Legal / business constants — parking lot.

These are NOT technical decisions. Every value here is a placeholder until a
lawyer / accountant signs off. See docs/LEGAL.md for the full question list.

Changing a value here must never require a code redesign.
"""

# Статус площадки: мы — информационный сервис / платёжный агент.
#   agent          — деньги идут через нас, мы отдаём партнёру комиссию
#   operator       — TBD, требует юриста
LEGAL_ROLE = "agent"

# Как идут деньги:
#   agent_collect    — эквайринг на наш счёт, потом выплата партнёру
#   direct_to_partner — деньги идут напрямую партнёру, мы выставляем счёт на комиссию
# NB: при УСН 6% и agent_collect налог может считаться со всей суммы платежа,
#     а не только с комиссии. Это серьёзный финансовый вопрос — см. docs/LEGAL.md §2.
MONEY_FLOW = "agent_collect"

# 152-ФЗ: персональные данные граждан РФ должны храниться на серверах в РФ.
DEPLOY_REGION = "RU"

# Версия публичных документов (оферта, политика конфиденциальности).
LEGAL_DOCS_VERSION = "2026.09-draft"

# Отмена: бесплатно за сколько часов до заселения.
CANCELLATION_FREE_BEFORE_HOURS = 24

# Штраф при поздней отмене (доля от total_amount, 0..1). TBD юристом.
CANCELLATION_PENALTY_RATE = 0.5

# Комиссия с удержанного штрафа при поздней отмене: платформа берёт свою долю
# с суммы, которую партнёр оставляет себе. False — штраф уходит партнёру без
# комиссии. Решение бизнеса, TBD.
COMMISSION_ON_PENALTY = True

# Плоская комиссия по умолчанию (тестовое значение, бизнес определит позже).
COMMISSION_DEFAULT_RATE = 0.12

# Налоговый режим: usn6 | usn15 | osno
TAX_SYSTEM = "usn6"

# 54-ФЗ: нужны онлайн-чеки (даёт эквайринг-провайдер).
REQUIRES_ONLINE_RECEIPT = True
