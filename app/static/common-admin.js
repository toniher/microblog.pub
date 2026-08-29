document.addEventListener('DOMContentLoaded', (ev) => {
  // Add confirm to "delete" button next to outbox objects. The prompt is
  // rendered (and translated) by the template on the form's `data-confirm`,
  // so this file holds no English of its own -- a federated delete and a
  // local-only one say different things. The fallback only covers a template
  // that predates the attribute.
  var forms = document.getElementsByClassName("object-delete-form")
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener('submit', (ev) => {
      var message = ev.currentTarget.getAttribute('data-confirm')
        || 'Do you really want to delete this object?';
      if (!confirm(message)) {
        ev.preventDefault();
      };
    });
  }
});
