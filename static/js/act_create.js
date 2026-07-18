document.addEventListener('DOMContentLoaded', () => {
    const formset = document.querySelector('[data-defect-formset]');
    if (!formset) {
        return;
    }

    const list = formset.querySelector('[data-defect-form-list]');
    const template = formset.querySelector('[data-empty-defect-form]');
    const addButton = formset.querySelector('[data-add-defect]');
    const totalForms = formset.querySelector('input[name$="-TOTAL_FORMS"]');

    if (!list || !template || !addButton || !totalForms) {
        return;
    }

    const reindexForms = () => {
        const blocks = list.querySelectorAll('.defect-form-block');
        blocks.forEach((block, index) => {
            block.querySelectorAll('input, select, textarea, label').forEach((element) => {
                ['name', 'id', 'for'].forEach((attribute) => {
                    const value = element.getAttribute(attribute);
                    if (!value) {
                        return;
                    }
                    element.setAttribute(attribute, value.replace(/defects-\d+-/g, `defects-${index}-`));
                });
            });
        });
        totalForms.value = blocks.length;
    };

    addButton.addEventListener('click', () => {
        const index = Number.parseInt(totalForms.value, 10);
        const html = template.innerHTML.replace(/__prefix__/g, index);
        list.insertAdjacentHTML('beforeend', html);
        totalForms.value = index + 1;
    });

    list.addEventListener('click', (event) => {
        const removeButton = event.target.closest('[data-remove-defect]');
        if (!removeButton) {
            return;
        }

        const block = removeButton.closest('.defect-form-block');
        if (!block || list.querySelectorAll('.defect-form-block').length <= 1) {
            return;
        }

        block.remove();
        reindexForms();
    });
});
