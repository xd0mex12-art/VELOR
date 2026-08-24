// ===== VELOR AI — поля форм из описания сервера =====
// Одно поле рисуется в трёх местах: в карточке подтверждения (входящие), в
// памяти бизнеса и в финансах. Пока копий было три, они успевали разойтись —
// правка суммы в одном экране вела себя иначе, чем в другом. Теперь описание
// поля приходит с сервера (entities.py), а рисует его эта функция.
//
// Ожидаемое поле: {name, label, type, required, hint, main, options:[{value,label}], value}
(function () {
  function vEsc(s){
    return String(s === null || s === undefined ? '' : s)
      .replace(/[&<>"']/g, function (c) {
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
      });
  }

  // Поля второго ряда (main === false) прячутся под кнопкой «Все поля»: в
  // карточке подтверждения человек проверяет сумму, а не заполняет анкету.
  window.fieldHTML = function (f, idPrefix) {
    var id = (idPrefix || 'f') + '-' + f.name;
    var val = (f.value === null || f.value === undefined) ? '' : f.value;
    var opts = f.options || [];
    var input;
    if (opts.length) {
      input = '<select id="' + vEsc(id) + '" name="' + vEsc(f.name) + '">'
        + (f.required ? '' : '<option value="">— не указано —</option>')
        + opts.map(function (o) {
            var sel = String(val) === String(o.value) ? ' selected' : '';
            return '<option value="' + vEsc(o.value) + '"' + sel + '>' + vEsc(o.label) + '</option>';
          }).join('')
        + '</select>';
    } else {
      input = '<input id="' + vEsc(id) + '" name="' + vEsc(f.name) + '" type="text"'
        + ' inputmode="' + (f.type === 'int' ? 'numeric' : 'text') + '"'
        + ' value="' + vEsc(val) + '" data-was="' + vEsc(val) + '">';
    }
    return '<div class="fld' + (f.main === false ? ' extra' : '') + '">'
      + '<label for="' + vEsc(id) + '">' + vEsc(f.label)
      + (f.required ? '<span class="req" title="обязательное">*</span>' : '') + '</label>'
      + input
      + (f.hint ? '<span class="hint">' + vEsc(f.hint) + '</span>' : '')
      + '<span class="was" style="display:none"></span>'
      + '</div>';
  };

  // Собрать значения формы. Пустое значение НЕ выбрасываем: для сервера это
  // осознанная очистка поля, а не «не трогай». Иначе стереть неверного
  // контрагента было бы нечем.
  window.formValues = function (form) {
    var out = {};
    form.querySelectorAll('input, select').forEach(function (el) {
      if (el.name) out[el.name] = String(el.value).trim();
    });
    return out;
  };

  // Пометка изменённых полей: правку видно до нажатия, а не только после.
  window.markDirty = function (form, prefix) {
    form.querySelectorAll('input, select').forEach(function (el) {
      el.addEventListener('input', function () {
        var changed = String(el.value) !== String(el.dataset.was || '');
        el.classList.toggle('dirty', changed);
        var was = el.closest('.fld') && el.closest('.fld').querySelector('.was');
        if (!was) return;
        was.style.display = changed ? 'block' : 'none';
        was.textContent = changed
          ? (prefix || 'Было: ') + (el.dataset.was || '(пусто)')
          : '';
      });
    });
  };
})();
