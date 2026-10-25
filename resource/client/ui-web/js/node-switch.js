(function ($) {
  'use strict';
  let switching = false;
  window.buttonSwitch = function (button) {
    if (switching) return;
    const selected = $(button);
    const index = selected.attr('data-node-index');
    if (!/^\d+$/.test(index || '')) return;
    const buttons = $('#nodeTable button[data-node-index]');
    const label = selected.text();
    switching = true;
    buttons.prop('disabled', true);
    selected.text('切换中…').attr('aria-busy', 'true');
    $.post('./act/changeNode.php', {nodenum: index})
      .done(function () {
        // Reload only after the broker confirms that the new proxy is ready.
        window.location.reload();
      })
      .always(function () {
        switching = false;
        buttons.prop('disabled', false);
        selected.text(label).attr('aria-busy', 'false');
      });
  };
})(jQuery);
