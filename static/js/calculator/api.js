/**
 * The persistence layer, replacing `js/storage.js` of the source repository.
 *
 * There is no file picker, no File System Access handle, no IndexedDB and no
 * document `revision`: the journal lives in the application's database and is
 * reached through the ordinary authenticated, CSRF-protected Django
 * endpoints under `/calculator/`. Calculation and rendering never call this
 * module's internals — they only ask it to load, create, confirm or unlock.
 */
(function () {
  var api = window.windingCalculator = window.windingCalculator || {};

  function csrfToken() {
    var match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  async function request(url, options) {
    var response;
    try {
      response = await fetch(url, Object.assign({ credentials: 'same-origin' }, options));
    } catch (error) {
      throw new Error('Нет связи с сервером. Проверьте подключение и повторите.');
    }
    if (response.redirected || response.status === 401 || response.status === 403) {
      throw new Error('Сессия завершена. Обновите страницу и войдите заново.');
    }
    var payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }
    if (!response.ok) {
      throw new Error((payload && payload.detail) || 'Не удалось сохранить данные. Повторите попытку.');
    }
    return payload;
  }

  function post(url, body) {
    return request(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
      body: JSON.stringify(body || {})
    });
  }

  /**
   * `entriesUrl` is `/calculator/entries/`; the per-entry routes hang off it,
   * which keeps the template down to one URL instead of four.
   */
  api.createJournalApi = function (entriesUrl) {
    function entryUrl(id, suffix) { return entriesUrl + encodeURIComponent(id) + suffix; }
    return {
      /** Every entry of the shared journal, current state from the database. */
      async load() { return (await request(entriesUrl, { method: 'GET' })).entries; },
      /** Create the case, or receive the one another user already created. */
      create(payload) { return post(entriesUrl + 'create/', payload); },
      confirmProduction(id, batchQuantity, actualBatchTimeHours) {
        return post(entryUrl(id, '/production/'), {
          batchQuantity: batchQuantity, actualBatchTimeHours: actualBatchTimeHours
        });
      },
      unlockProduction(id) { return post(entryUrl(id, '/production/unlock/'), {}); }
    };
  };
})();
