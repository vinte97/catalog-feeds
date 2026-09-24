# Мануал: YML-фиды для B24U (для агента)

Каноническая копия навыка агента: `~/.cursor/skills/b24u-yml-feed/`.

Кратко: собрать публичный `feed.xml` (Yandex Market YML), **закоммитить и запушить** в git, чтобы URL на GitHub Pages можно было вставить в B24U.

## Как поручить агенту

Примеры формулировок:

- «По скиллу b24u-yml-feed спарси эти URL и выложи фид в catalog-feeds как `shopname`»
- «Собери YML из этого TSV, лимит 500, опубликуй в git»
- «На сайте example.ru возьми раздел /catalog/foo/, до 300 позиций с фото, feed на GitHub Pages»

Агенту достаточно: сайт или список ссылок, slug, лимит. Публикация в git — часть задачи по умолчанию.

## Почему не CSV

B24U рисует превью только из тега `<picture>` в YML. Колонка `image` в CSV в карточку не попадает.

## Карточка (порядок тегов)

```text
name → url → picture → currencyId → categoryId → vendor → vendorCode → price? → quantity? → description
```

- Валюта: только `RUB`
- Без `<param>` (иначе фото часто остаётся ссылкой)
- Уникальный `https` URL на оффер
- Не подставлять `no_photo` и чужие фото с страницы

Эталоны:

- https://stepanenkoviktor0110-boop.github.io/prosps-feed/feed.xml
- https://stepanenkoviktor0110-boop.github.io/shkafulkin-proxy-feed/feed.xml

## Цена

- Есть число → `<price>число</price>`.
- Нет цены на сайте / «по запросу» → всё равно `<price>0</price>` + в description `Цена: по запросу`.
  Иначе B24U: `FEED_ALL_FILTERED` / «Нет цены», карточек не будет.
- У оффера без розничной цены также: `available="true"`, `vendorCode`, `quantity`.

## Публикация в git (обязательно)

| | |
|---|---|
| Репо | `vinte97/catalog-feeds` (`main`) |
| Локально | `/Users/yt/WORK/catalog-feeds/<slug>/feed.xml` |
| Публично | `https://vinte97.github.io/catalog-feeds/<slug>/feed.xml` |
| Конвертер xlsx | `build_yml.py` |

Агент должен сам:

1. Записать `feed.xml` и обновить `index.html`.
2. `git add` + `git commit` + `git push` в **catalog-feeds** (не в ai-platform).
3. Push так:

```bash
cd /Users/yt/WORK/catalog-feeds
git -c credential.helper='!/opt/homebrew/bin/gh auth git-credential' push origin HEAD
```

4. Отдать URL Pages и напомнить реимпорт в кабинете B24U.

Без push задача не завершена.

Полные правила, маппинг полей и скелет парсера: скилл `b24u-yml-feed` (`SKILL.md` + `reference.md`).
