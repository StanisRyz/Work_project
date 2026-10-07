# Доски: что взять у западных трекеров (продолжение исследования)

Исследование от 7 октября 2026. Изучены по официальной документации и журналам
изменений 2024–2026: Linear, Jira (вместе с Work Management и Automation),
Trello (Butler, зеркала карточек, Inbox/Planner), Asana, ClickUp, Monday,
Notion Projects, Plane, GitHub Projects.

Height закрылся 24 сентября 2025 года, поэтому не рассматривается
([AlternativeTo](https://alternativeto.net/news/2025/3/height-project-management-tool-to-shut-down-by-september-2025/)).

Исходные документы:
- `tasksandreports/boards/research/features.md` — что уже предложено (A–D);
- раздел «Boards» в `AGENTS.md` — что уже сделано.

Здесь только новое. Если пункт существенно уточняет что-то из features.md,
это сказано прямо.

Объём:
- **S** — меньше этапа;
- **M** — один этап;
- **L** — два этапа и больше.

## Подзадачи — лучшие приёмы

1. **Создание пачкой.** В Linear подзадачу создают кнопкой «+ Add sub-issues»
   под описанием. Если вставить список названий, появится сразу несколько
   подзадач. Подзадачей можно сделать и сообщение («new sub-issue from
   comment»), и выделенный пункт списка или чек-листа.
   [Linear: parent and sub-issues](https://linear.app/docs/parent-and-sub-issues)
2. **Что наследуется** (Linear). Подзадача берёт у родителя команду, приоритет
   и проект, а цикл — если создана в активном статусе. Метки не наследуются.
   Исполнитель наследуется, только если родитель назначен на вас или у всех
   подзадач уже один исполнитель. [там же](https://linear.app/docs/parent-and-sub-issues)
3. **Автозакрытие в обе стороны** (Linear, с 6 сентября 2024). Две отдельные
   настройки команды:
   - все подзадачи выполнены → родитель выполнен;
   - родитель выполнен → оставшиеся подзадачи выполнены.

   [Linear: changelog](https://linear.app/changelog/timeline),
   [docs](https://linear.app/docs/parent-and-sub-issues)
4. **Закрытие родителя с открытыми подзадачами** (ClickUp). Система спрашивает,
   закрыть ли подзадачи тоже: не молча и не запретом.
   [ClickUp: Intro to subtasks](https://help.clickup.com/hc/en-us/articles/6309825777943-Use-subtasks)
5. **Jira.** Родитель показывает панель дочерних элементов с «% done» по
   категории статуса «Done». Чтобы родитель закрывался сам, есть готовый шаблон
   автоматизации «When all sub-tasks are done → move parent to done».
   [JRACLOUD-80273](https://jira.atlassian.com/browse/JRACLOUD-80273),
   [Atlassian KB](https://support.atlassian.com/automation/kb/close-the-parent-issue-when-all-its-sub-tasks-are-closed/)
6. **Как показывать подзадачи** (Linear, 3 апреля 2025). Пользователь выбирает
   видимые свойства подзадач, порядок и «скрывать выполненные». Настройка
   личная и действует во всех задачах. Фильтры умеют отбирать «только верхний
   уровень», «только подзадачи» и «с подзадачами».
   [Linear changelog 2025-04-03](https://linear.app/changelog/2025-04-03-collapsed-issue-history)
7. **На доске проекта подзадач по умолчанию нет** (Asana). Включаются
   переключателем, но в «Мои задачи», «Входящих» и поиске видны всегда. Так
   доска не засоряется, а исполнитель свою подзадачу не теряет.
   [Asana: subtasks](https://asana.com/resources/asana-tips-subtasks)
8. **Сводка по подзадачам у родителя** (Monday). У подэлементов свои колонки.
   Родитель собирает их в сводку: числа — SUM/MIN/MAX, даты — MIN/MAX (по
   умолчанию MAX), статусы — счёт по значениям. Многоуровневые доски — до
   5 уровней.
   [monday: multi-level boards](https://developer.monday.com/api-reference/docs/working-with-multi-level-boards)
9. **Пределы и колонки в GitHub.** До 100 подзадач у родителя и до 8 уровней
   вложенности. В Projects есть поля «Parent issue» (группировка и фильтр
   `parent-issue:`) и «Sub-issue progress» (сколько выполнено).
   [GitHub: sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues),
   [поля](https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-parent-issue-and-sub-issue-progress-fields)
10. **Пункт чек-листа → карточка** (Trello). Название, срок и исполнитель
    пункта переходят в новую карточку. Своих подзадач в Trello нет.
    [Trello: checklists](https://support.atlassian.com/trello/docs/adding-checklists-to-cards/)
11. **Зависимости между подзадачами** (Asana). Подзадачи сами по себе друг от
    друга не зависят, связь «после» задаётся явно. Когда предшествующая
    выполнена, исполнитель зависимой получает уведомление.
    [Asana: auto-shifting dates](https://help.asana.com/s/article/auto-shifting-dates-for-dependent-tasks)
12. **Копирование и архив** (Linear). «Duplicate» с переключателем «Include
    sub-issues». Родитель не уходит в автоархив, пока не закрыты все его
    подзадачи.
    [Linear: delete/archive](https://linear.app/docs/delete-archive-issues)
13. **Подводный камень Plane.** Подзадачи удаляются вместе с родителем и не
    копируются при дублировании.
    [Plane: work items](https://docs.plane.so/core-concepts/issues/overview)

**Вывод для нас:**
- подзадача — это дочерняя `BoardCard` со своей задачей `BOARD`;
- на плитке родителя — «Подзадачи 1/3», в теле подзадачи — ссылка на родителя;
- по умолчанию подзадача не занимает место в колонках, но видна в «Мои задачи»
  и в «Таблице»;
- закрытие родителя с открытыми подзадачами спрашивает «закрыть и их?»;
- автозакрытие родителя — опция доски;
- «пункт чек-листа → подзадача» и «вставить список → N подзадач».

## Файлы в комментариях — лучшие приёмы

1. **Три способа приложить файл в одном поле сообщения:** скрепка,
   перетаскивание и Ctrl+V скриншота.
   - Linear: скрепка, Ctrl+Shift+A, перетаскивание;
   - GitHub: перетаскивание, вставка из буфера, скрепка; файл грузится сразу
     и получает обезличенный адрес;
   - Trello: картинку можно вставить Ctrl+V прямо на плитку или в открытую
     карточку, а файл перетащить на плитку, не открывая её.

   [Linear: comments](https://linear.app/docs/comment-on-issues),
   [GitHub: attaching files](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files),
   [Trello: attachments](https://support.atlassian.com/trello/docs/adding-attachments-to-cards/)
2. **Сообщение — это текст плюс несколько файлов одним блоком** (Asana:
   «a block of text and one or more attachments grouped together»).
   [Asana: comments and attachments](https://help.asana.com/s/article/task-comments-and-attachments)
3. **Общая галерея файлов карточки, даже если файлы пришли из чата.** Во
   вкладке «Files» карточки Monday собраны файлы и из колонки «Файл», и из
   ленты обновлений.
   [monday: manage files](https://support.monday.com/hc/en-us/articles/115005339505)

   Обратный пример — Asana. Её «Files view» пропускает файлы из комментариев,
   и пользователи на это жалуются.
   [форум Asana](https://forum.asana.com/t/ability-to-view-all-files-attached-to-subtasks-or-comments-in-files-tab/1152075)
4. **Три вида списка вложений** (Jira):
   - «strip» — лента превью;
   - «grid» — сетка превью;
   - «list» — компактно, с размерами.

   Больше 150 файлов — всегда список. Есть «Download all» и «Delete all».
   [Jira: strip/list view](https://support.atlassian.com/jira-software-cloud/docs/switch-between-the-strip-and-list-view-for-attachments)
5. **Ссылка на уже приложенный файл без повторной загрузки** (Jira). Команды
   `/file`, `/attach`, `/reference` или «Attachments on this work item» в
   редакторе комментария.
   [Jira: insert an attachment](https://support.atlassian.com/jira-software-cloud/docs/insert-an-attachment-already-on-an-issue/)
6. **Все вложения проекта на одной странице** (Jira business projects).
   Фильтры: «Added by», тип и дата. Файл можно скачать, даже не зная, в какой
   он задаче.
   [Jira: view all attachments](https://support.atlassian.com/jira-software-cloud/docs/view-all-attachments-in-a-business-project/)
7. **Просмотр большинства документов внутри приложения** (Trello), плюс
   миниатюра на карточке для тех, кому нужно.
   [Trello: attachments](https://support.atlassian.com/trello/docs/adding-attachments-to-cards/)
8. **Лимиты разные по типу файла** (GitHub): изображения 10 МБ, видео 10/100 МБ,
   прочее 25 МБ. Файлы приватного репозитория видят только те, у кого к нему
   доступ — то же правило «читает тот, кто читает источник».
   [GitHub: attaching files](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files)
9. **Из сообщения с файлом — подзадача или новая карточка** (Linear: «create a
   new issue or sub-issue from the comment»).
   [Linear: comments](https://linear.app/docs/comment-on-issues)

**Вывод для нас:**
- в форме «Чата» нужны скрепка, перетаскивание на чат и Ctrl+V;
- файлы — строки, привязанные и к сообщению, и к задаче. Тогда «Вложения»
  документации, «Задачи» и права на скачивание не меняются;
- миниатюры картинок и PDF показываются через защищённый inline-просмотр с
  проверкой прав на каждый запрос, как `documents:system_preview`;
- вместо вкладки «Файлы» — переключатель «только с файлами» в «Чате» и счётчик
  «📎 N» в шапке карточки, чтобы общий список файлов карточки не потерялся
  (урок Asana);
- удалённый файл оставляет в сообщении пометку «файл удалён», само сообщение
  остаётся.

## Новые идеи

В таблице нет того, что уже сделано или есть в features.md. Пометка
«уточняет X» — пункт развивает идею X из features.md. Порядок — по важности
для ОП/ПДО.

| # | Что | Где есть | Зачем ОП/ПДО | Объём | Источник |
| --- | --- | --- | --- | --- | --- |
| W1 | **Форма заявки и «Входящие на разбор».** Сотрудник без членства на доске подаёт заявку по форме (поля доски, обязательные отмечены). Карточка попадает во «Входящие», ответственный: принять, отклонить с причиной, «дубль ZAP-…» или «отложить до…». Уточняет B2, B3 | Jira forms (business), Linear Asks web forms + Triage (accept / decline / duplicate / snooze), Asana forms с ветвлением, Notion forms, Plane Intake | Цеха, ОТК и снабжение подают в ОП/ПДО заказ или вопрос в одном формате. ПДО видит очередь непринятого, а не кашу в «Сделать» | M–L | [Linear Triage](https://linear.app/docs/triage), [Linear web forms](https://linear.app/docs/linear-asks-web-forms), [Jira forms](https://support.atlassian.com/jira-work-management/docs/share-your-form/), [Asana branching](https://help.asana.com/s/article/how-to-use-forms-branching), [Plane Intake](https://docs.plane.so/core-concepts/intake) |
| W2 | **Связи карточек.** Виды: «блокирует», «заблокирована», «связана», «дубль». У заблокированной на плитке оранжевый флажок; когда блокирующая закрыта, связь становится «связана», а исполнитель получает «можно начинать». «ZAP-12» в чате или описании сама делает связь «связана». «Дубль» закрывает карточку со ссылкой на основную. Не то же, что B1: там флаг без ссылки | Linear, Jira, Plane, Asana (уведомление о готовности) | «Заказ ждёт КД / материала / другого заказа» — со ссылкой, а не словами | M | [Linear: relations](https://linear.app/docs/issue-property), [Jira: link work items](https://support.atlassian.com/jira-software-cloud/docs/link-issues), [Asana](https://help.asana.com/s/article/auto-shifting-dates-for-dependent-tasks) |
| W3 | **Напоминание о сроке.** Исполнителям и подписчикам — «срок завтра» и «просрочено» в колокольчике, по желанию на почту. Запуск раз в день, как `document_review_reminders` | Linear (due date notifications), Monday «when date arrives», Trello Butler due date commands, Jira scheduled rules | Сейчас срок только подсвечивается, сигнала никто не получает. Не путать с C2 (дайджест) — это точечное событие по карточке | S | [Linear: due dates](https://linear.app/docs/issue-property), [monday: reminders](https://support.monday.com/hc/en-us/articles/360000227739-Alerts-and-Reminders-with-Automations) |
| W4 | **Нормативный срок (SLA) по правилу.** Например, «Приоритет = Срочно → 2 рабочих дня». Значок меняет цвет: серый → жёлтый → оранжевый → красный. Уведомление за сутки до нарушения, а после выполнения — «уложились / не уложились». Фильтр «под угрозой / нарушен». Уточняет A6 (там порог по колонке, здесь срок карточки по правилу) | Linear SLA (рабочие дни, порядок правил, отчёт в Insights) | Норматив запуска по типу заказа и честная «доля в срок» для руководителя. `ecosystem.workdays` уже есть | M | [Linear: SLAs](https://linear.app/docs/issue-property) |
| W5 | **Сохранённые виды доски.** Набор фильтров (поля, «Мои», «Застрявшие», поиск) сохраняется под именем и становится кнопкой над колонками: общей для доски или личной. Подписка «сообщить, когда карточка попала в вид» | Jira quick filters (кнопки на доске), Linear custom views + view subscriptions, ссылка с фильтрами | «Срочные ОП», «Стоп», «Ждут номера заявки» — одним щелчком. Руководитель подписан на «Срочные» | M | [Jira: quick filters](https://support.atlassian.com/jira-software-cloud/docs/configure-quick-filters/), [Linear: custom views](https://linear.app/docs/custom-views) |
| W6 | **Правила доски «когда → то»** из закрытого списка рецептов. Например: вошла в колонку → поставить поле или добавить подписчика; поле стало «Стоп» → уведомить автора; срок наступил → уведомить. Обобщает A3 | Jira Automation, Trello Butler, Monday recipes (200+), Notion database automations, Asana rules | Убирает ручные шаги между этапами без программиста. 5–6 рецептов закрывают основное | M–L | [Trello Butler](https://www.atlassian.com/blog/trello/butler-power-up-trello-automation), [Notion automations](https://www.notion.com/help/database-automations), [monday](https://support.monday.com/hc/en-us/articles/360000227739-Alerts-and-Reminders-with-Automations) |
| W7 | **Кнопка-действие на карточке.** Например, «Передать в ПДО»: переместить, задать поле, назначить, оставить комментарий — одним нажатием. Владелец настраивает до N кнопок на доске | Trello Butler card buttons / board buttons | Передача этапа одинаково у всех и без забытых шагов | M | [Trello Butler](https://www.atlassian.com/blog/trello/butler-power-up-trello-automation) |
| W8 | **Назначенное сообщение (поручение в чате).** Сообщению назначают ответственного; «Решено» он отмечает со своим именем, автор получает уведомление. Списки «назначено мне / мной». Опция: не завершать карточку, пока есть нерешённые | ClickUp Assigned comments, Linear «resolve thread» | «Уточнить у ОП габарит» не тонет в чате. Видно, кто кому что должен | M | [ClickUp: assign comments](https://help.clickup.com/hc/en-us/articles/6311126397591-Assign-comments), [Linear: threads](https://linear.app/docs/comment-on-issues) |
| W9 | **«Просмотрено» у сообщений.** Под сообщением значок «глаз» и список тех, кто видел. Дешёвый вариант — время последнего прочтения карточки на человека | Monday Updates «Seen by» | ОП видит, что ПДО прочитал изменение заказа, — меньше звонков «вы видели?» | S–M | [monday: Updates](https://support.monday.com/hc/en-us/articles/115005900249) |
| W10 | **Сумма числового поля в заголовке колонки** и в «Таблице»: Σ «Кол-во, шт», «Сумма заказа» | GitHub Projects «Field sum» (доска и группы таблицы) | Сколько изделий или денег стоит на каждом этапе — без выгрузки в Excel | S | [GitHub: board layout](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/customizing-the-board-layout) |
| W11 | **«Напомнить мне» и «Отложить».** Карточку или сообщение откладывают до даты и времени, и в нужный момент приходит личное уведомление. Уведомление можно отложить в колокольчике | Linear Inbox (H — snooze / remind), ClickUp «Remind me» из комментария, Linear Triage snooze | «Вернуться к заказу, когда придёт металл» — без бумажек и календаря | M | [Linear: inbox](https://linear.app/docs/inbox), [ClickUp: reminders](https://help.clickup.com/hc/en-us/articles/34058632971543-Create-a-reminder) |
| W12 | **Одна карточка на двух досках (зеркало).** Карточка ОП показывается на доске ПДО с меткой исходной доски, а правится оригинал. Без доступа к источнику — «запросить доступ». Архив источника помечается на зеркале | Trello card mirroring, Asana multi-homing, ClickUp «Tasks in Multiple Lists» | Каждый отдел ведёт свою доску, заказ при этом один — без двойного ввода | L | [Trello: mirroring](https://support.atlassian.com/trello/docs/mirroring-cards/), [ClickUp FAQ](https://help.clickup.com/hc/en-us/articles/6309521498263-Subtasks-in-Multiple-Lists-FAQ) |
| W13 | **Перенос на другую доску с новым номером.** Старый номер продолжает открываться, ищется и ведёт на новую карточку. Уточняет A7: у нас перенос только между поддосками | Linear «Move to team» (новый ID, старые URL и ID работают, Ctrl+Z) | «ZAP-12» из старого письма не ломается после передачи заказа на другую доску | M | [Linear: editing issues](https://linear.app/docs/editing-issues) |
| W14 | **«Следующая карточка» и «Копировать».** Из карточки создаётся связанная следующая с теми же полями и исполнителями по выбору. Копирование — вместе с чек-листом и подзадачами | Asana «Create follow-up task», Linear «Duplicate» + «Include sub-issues» | Повторный заказ того же клиента или следующий этап — без ручного переноса полей | S–M | [Asana forum](https://forum.asana.com/t/create-follow-up-task/14408), [Linear](https://linear.app/docs/parent-and-sub-issues) |
| W15 | **Создание карточки письмом.** Письмо на адрес доски → карточка во «Входящих»: тема — название, текст — описание, вложения — файлы | Linear (email intake для Asks), Trello Inbox (`inbox@app.trello.com`) | Заказы и уточнения ОП часто приходят письмом. Нужен входящий ящик (IMAP); сейчас есть только исходящий SMTP | M–L | [Linear: Asks email](https://linear.app/docs/linear-asks-email), [Trello: new Trello](https://www.atlassian.com/blog/trello/new-trello-is-here) |
| W16 | **«Было → стало» по полям и откат описания.** В «Логе» показывается старое и новое значение поля; описание можно вернуть к прежней версии. Уточняет журнал: сейчас там только имена полей. По `AGENTS.md` значения в `details` писать нельзя, так что нужна отдельная таблица с правом чтения доски | Jira History (original / new value), Linear «Issue description history» | Споры «кто поменял срок изготовления и с какого на какой» решаются за минуту | M | [Linear: editing issues](https://linear.app/docs/editing-issues), [Jira: activity/history](https://support.atlassian.com/jira-software-cloud/docs/what-are-the-different-types-of-activity-on-an-issue/) |
| W17 | **«Мои карточки» по группам**: «Просрочено / Сегодня / Дальше / Без срока / Заблокировано», плюс «закрепить на сегодня». Уточняет «Мои задачи» на главной | ClickUp My Tasks (Today / Overdue / Next / Unscheduled), Linear My Issues (срочное, SLA, блокеры по порядку) | Человек начинает день с правильной карточки, а не с самой новой | S–M | [Linear: my issues](https://linear.app/docs/my-issues), [ClickUp: My Tasks](https://help.clickup.com/hc/en-us/articles/31007956275863-My-Tasks) |
| W18 | **Командная палитра Ctrl+K.** Найти карточку по номеру или названию и сразу выполнить действие: переместить, назначить, срок, подписаться. Выбор нескольких — X и Shift+стрелки, «выделить все после фильтра» и общее действие. Уточняет B7 и B13 | Linear (Cmd/Ctrl K, X, Shift, Cmd/Ctrl A + действие) | Разбор доски на планёрке — с клавиатуры, без десятка щелчков | M | [Linear: select issues](https://linear.app/docs/select-issues) |
| W19 | **Подборка карточек по ссылке**: `…?cards=ZAP-12,ZAP-15,ZAP-21` открывает список только этих карточек | Linear (`/issues/ENG-123,ENG-456`) | Ссылка в письме или протоколе: «обсуждаем эти пять заказов» | S | [Linear: custom views](https://linear.app/docs/custom-views) |
| W20 | **Черновик неотправленного сообщения.** Текст в «Чате» сохраняется при закрытии карточки и предлагается снова. Через `form_drafts.js` (`localStorage`), ничего не уходит на сервер | Linear (unsent comments видны в карточке и в «Drafts») | Начатое сообщение не теряется при переходе к соседней карточке | S | [Linear: comments](https://linear.app/docs/comment-on-issues) |
| W21 | **Свёртка однотипных событий «Лога»:** подряд идущие похожие записи складываются в одну строку «ещё N» | Linear «Collapsed issue history» (3 апреля 2025) | Длинная карточка заказа читается быстрее | S | [Linear changelog](https://linear.app/changelog/2025-04-03-collapsed-issue-history) |

Самые полезные для запуска заказов: W1 + W2 + W3 + W5. Они закрывают приём
заявок, ожидание чужой работы, сроки и «руководителю одним щелчком», почти не
трогая модель данных. W4 и W6 — следующий шаг, когда заработают поля и
правила.

## Не брать

- AI-сводки и агенты (Linear Agent, Trello AI-разбор писем) — нужен внешний
  интернет и платные модели.
- Интеграции Slack, Teams и календарей (Trello Planner) — на заводе их нет.
- Спринты, оценки в очках, Git-автоматизации — то же, что в разделе D
  features.md.
