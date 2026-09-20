# Провайдер ИИ-объяснений — интерфейс и подмена (S5-01)

Состояние: 20.09.2026. Интерфейс согласован с B в #36; реального провайдера **нет** — до подтверждения заказчиком трёх вещей: какой провайдер, какие данные допустимо отправлять наружу (тексты комментариев — отдельно), лимит расходов. Пока их нет, `AI_PROVIDER=off` (по умолчанию) или `stub` для локальных проверок.

## Границы

| Слой | Модуль | Делает | Не делает |
| --- | --- | --- | --- |
| domain (B) | `ai_context.py` | Собирает контекст одного контрагента и `Request` (инструкция + JSON) | Не знает провайдера |
| domain (B) | `ai_review.py` | Проверяет ответ: схема, ссылки, новые числа/даты, приоритет → `Explanation` или `Rejected` | Не зовёт модель |
| application (A) | `recommendation.py` | `RecommendationProvider` (протокол), `AiLimits`, `AiAnswer`, `AiUnavailable`, `request_explanation()` — построить запрос, спросить, проверить, вернуть `ExplanationOutcome` | Не решает, что показать в отчёте (S5-04) и не хранит (S5-02) |
| infrastructure (A) | `llm/stub.py` | Подмена модели: ответ из слов запроса, задержка, сбой по сценарию | Сети нет |
| runtime (A) | `ai.py` | `build_recommendation_provider(settings)` по `AI_PROVIDER` | — |

## Интерфейс провайдера

```python
class RecommendationProvider(Protocol):
    name: str      # "stub", позже — имя реального провайдера
    model: str     # точная версия модели; попадает в версию сохранённого объяснения
    async def explain(self, request: Request, limits: AiLimits) -> AiAnswer
```

- `Request` — из `domain/ai_context.build_request`: инструкция, контекст (`dict`, готов к JSON), версии.
- `AiLimits(timeout_seconds=30, max_request_chars=40_000, max_output_tokens=800)` — лимиты одного запроса; полная политика (повторы, бюджет на проверку, выключатель) — S5-03.
- `AiAnswer(text, provider, model, input_tokens, output_tokens, latency_seconds)` — ответ **как пришёл** плюс стоимость. Провайдер ответ не разбирает.
- Ожидаемые отказы — `AiUnavailable(code, message)` с кодом `unavailable | timeout | rate_limited | refused | budget`; ошибки программирования — обычные исключения (воркер зафиксирует проверку как `failed`).
- Провайдер **не логирует** тело запроса и ответ: там данные заказчика.

`request_explanation(provider, context, limits)` → `ExplanationOutcome(provider, model, versions, answer, explanation | rejected | error)`; `status` — `accepted`, `rejected:<код S5-06>`, `unavailable:<код>`. Запрос больше `max_request_chars` наружу не уходит (`unavailable:budget`). `versions = "<provider>:<model>:i<инструкция>:c<контекст>:s<схема ответа>:r<правила>"` — ключ хранения в S5-02: любая смена версии = новый ключ.

## Хранение ответов (S5-02)

Конвейер (`analysis_pipeline._explain`) после расчёта оценок строит контекст каждой организации (`build_context(..., reference="row-N")`, комментарии — только при `AI_SEND_COMMENTS=true`), спрашивает провайдера через `request_explanation` и сохраняет ответ шагом `(ИНН, "explanation", versions)` — `application/explanations.StoredExplanation`: статус, версии, провайдер/модель, ссылка, сырой текст ответа, токены и задержка, принятое объяснение или код отклонения. Сохраняются **принятые и отклонённые** ответы (окончательные); недоступность модели шага не оставляет — следующий прогон может спросить снова (границы — S5-03).

Правила:
- ключ включает все версии (`provider:model:i<инструкция>:c<контекст>:s<схема>:r<правила>`): смена любой — новый ключ, старое остаётся в истории;
- возобновлённая проверка и повторный захват читают шаг и модель не зовут; повторная выдача отчёта (`/report`) конвейер не запускает вовсе — ИИ не вызывается заново;
- принятое объяснение связано с оценкой через `ReportRow.explanation`, версия — в «О проверке» (`ReportMeta.ai_version`: «stub stub-1; контекст 1, инструкция 1»); вывод текста объяснения в отчёт — S5-04;
- шаги живут в хранилище проверки и удаляются с ней по сроку хранения (S6-02).

## Подмена (`AI_PROVIDER=stub`)

`StubRecommendationProvider(script=default_answer, delay_seconds=0, failure=None)`:

- `default_answer` строит ответ только из слов запроса — метка приоритета и `next_step` правил, `grounds` = ID оснований сигналов, обещаний нет — поэтому он проходит проверку S5-06 на всех демосценариях (тест).
- `script` — любой текст (тест на отклонение), `delay_seconds` больше `timeout_seconds` → `timeout`, `failure` — заданный `AiUnavailable`.
- `requests` — что было отправлено, для тестов «в запросе нет ИНН/чужих данных».

## Что записывать перед подключением реального провайдера

1. Провайдер и модель (`name`, `model`), способ доступа (ключ — только в окружении, как `CHECKO_API_KEY`).
2. Какие поля контекста разрешены заказчиком: ИНН по умолчанию не отправляется (`counterparty_ref`), комментарии — только при явном согласии (`include_comments`). **Известный пробел:** ID фактов имеют вид `<ИНН>:<раздел>:<вид>` (S1-05), поэтому ИНН всё же попадает в запрос через `facts[].id` и `signals[].fact_ids` — тест `test_request_body_does_not_contain_the_inn_anywhere` помечен `xfail` до маскирования ID в контексте (B, #36).
3. Лимит расходов на проверку и на день (S5-03) и как он считается из `input_tokens`/`output_tokens`.
4. Клиент добавляется в `infrastructure/llm/<provider>.py`, реализует протокол, отображает ошибки транспорта на коды `AiUnavailable`, и регистрируется в `runtime/ai.py` новым значением `AI_PROVIDER`.
