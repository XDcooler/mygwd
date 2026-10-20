(function ($) {
  'use strict';
  const token = document.querySelector('meta[name="gwd-csrf"]');
  $.ajaxPrefilter(function (options, original, xhr) {
    const url = new URL(options.url, window.location.href);
    if (url.origin === window.location.origin && !/^(GET|HEAD)$/i.test(options.type)) {
      xhr.setRequestHeader('X-GWD-CSRF', token ? token.content : '');
    }
  });
  $(document).ajaxError(function (event, xhr, settings) {
    const url = new URL(settings.url, window.location.href);
    if (url.origin !== window.location.origin) return;
    if (/^(GET|HEAD)$/i.test(settings.type) && !/(?:act|auth)\//.test(url.pathname)) return;
    const error = xhr.responseJSON && xhr.responseJSON.error;
    const message = error || (xhr.responseText || '').slice(0, 400) || '操作未完成，请检查服务状态和日志。';
    let box = document.getElementById('gwd-operation-error');
    if (!box) {
      box = document.createElement('div');
      box.id = 'gwd-operation-error';
      box.className = 'alert alert-danger';
      box.setAttribute('role', 'alert');
      document.body.prepend(box);
    }
    box.textContent = 'HTTP ' + xhr.status + '：' + message;
    box.scrollIntoView({block:'nearest'});
    $('.spinner-border').removeClass('spinner-border spinner-border-sm');
  });
})(jQuery);
