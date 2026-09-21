# YML-фиды каталогов для B24U

Рабочий пример — Яндекс Маркет YML (`<yml_catalog>` / `<offer>` / `<picture>`), как у [Шкафулькина](https://stepanenkoviktor0110-boop.github.io/shkafulkin-proxy-feed/feed.xml).

Таблица — CSV. Виджет B24U рисует фото карточки только из тега `<picture>`. Колонка `image` в CSV в карточку не попадает, поэтому картинки не отображаются, даже когда URL в ячейке есть.

В виджет подключать три фида:

| Лист | URL |
|---|---|
| Южный | `yuzhniy/feed.xml` |
| Зелёный магазин | `zelenyi-magazin/feed.xml` |
| Orac Decor | `oracdecor/feed.xml` |

Остальные листы той же книги (`yard76`, `smazki`, `nipol`) лежат рядом. У Yard76 и НИПОЛ в таблице нет URL фото, поэтому `<picture>` там не из чего собрать.

Пересборка из xlsx-выгрузки таблицы:

```bash
python3 build_yml.py /path/to/feeds.xlsx .
```

Что переносится в YML:

- `image` → один или несколько `<picture>` (абсолютный `http(s)`).
- `price` / `old_price` → `<price>` / `<oldprice>`, только если это число. «По запросу» остаётся текстом в описании, выдуманная цена не подставляется.
- Валюта всегда `RUB`. `RUR` виджет не рендерит.
- `available`: `да` / `True` → `available="true"`, `нет` / `False` → `available="false"`.
- `category` со слэшами → дерево `<category parentId>`.
- `brand` → `<vendor>`, артикул → `<vendorCode>`.
- Остальные колонки → `<param name="…">`.

В исходной таблице нет фото у Yard76 (0 из 100) и НИПОЛ (0 из 43). В YML у этих офферов не будет `<picture>`, пока в лист не попадут прямые ссылки на jpg/png/webp.
