document.addEventListener('DOMContentLoaded', () => {
    const actForm = document.querySelector('.act-form');
    const formset = document.querySelector('[data-defect-formset]');
    if (!actForm || !formset) {
        return;
    }

    const list = formset.querySelector('[data-defect-form-list]');
    const template = formset.querySelector('[data-empty-defect-form]');
    const addButton = formset.querySelector('[data-add-defect]');
    const totalForms = formset.querySelector('input[name$="-TOTAL_FORMS"]');

    if (!list || !template || !addButton || !totalForms) {
        return;
    }

    const setFieldError = (field, message = '') => {
        const label = field.closest('label');
        if (!label) {
            return;
        }
        let error = label.querySelector('[data-client-field-error]');
        if (message) {
            label.classList.add('field--invalid');
            if (!error) {
                error = document.createElement('span');
                error.className = 'client-field-error';
                error.dataset.clientFieldError = '';
                label.append(error);
            }
            error.textContent = message;
        } else {
            label.classList.remove('field--invalid');
            error?.remove();
        }
    };

    const validateField = (field, showRequired = false) => {
        const value = field.value.trim();
        let message = '';
        if (showRequired && field.required && !value) {
            message = 'Заполните поле.';
        } else if (value && ['nomenclature', 'kd_designation'].some((name) => field.name.endsWith(name)) && !/^[А-Яа-яЁё0-9.-]+$/.test(value)) {
            message = 'Допустимы только русские буквы, цифры, точки и тире.';
        } else if (value && ['order_number', 'znp_number', 'party_number'].some((name) => field.name.endsWith(name)) && !/^[0-9/-]+$/.test(value)) {
            message = 'Допустимы только цифры, дефис и слэш.';
        } else if (value && field.type === 'number' && (!/^\d+$/.test(value) || Number(value) < 0)) {
            message = 'Введите целое неотрицательное число.';
        }

        if (!message && field.name.endsWith('nonconforming_quantity')) {
            const block = field.closest('.defect-form-block');
            const checkedField = block?.querySelector('input[name$="checked_quantity"]');
            if (checkedField?.value && Number(value) > Number(checkedField.value)) {
                message = 'Количество несоответствующей продукции не может превышать количество проверенной продукции.';
            }
        }
        setFieldError(field, message);
        return !message;
    };

    const validateBlockQuantities = (block) => {
        block?.querySelectorAll('input[name$="checked_quantity"], input[name$="nonconforming_quantity"]')
            .forEach(validateField);
    };

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

    actForm.addEventListener('input', (event) => {
        const field = event.target;
        if (field.matches('input, textarea')) {
            validateField(field, false);
            validateBlockQuantities(field.closest('.defect-form-block'));
        }
    });

    actForm.addEventListener('change', (event) => {
        const field = event.target;
        if (field.matches('select, input[type="date"]')) {
            validateField(field, false);
        }
    });

    actForm.addEventListener('blur', (event) => {
        if (event.target.matches('input, select, textarea')) {
            validateField(event.target, false);
        }
    }, true);

    actForm.addEventListener('submit', (event) => {
        const fields = actForm.querySelectorAll('input:not([type="hidden"]), select, textarea');
        const isValid = [...fields].every((field) => validateField(field, true));
        if (!isValid) {
            event.preventDefault();
            actForm.querySelector('.field--invalid input, .field--invalid select, .field--invalid textarea')?.focus();
        }
    });

    list.addEventListener('click', (event) => {
        const removeButton = event.target.closest('[data-remove-defect]');
        if (!removeButton) {
            return;
        }

        const block = removeButton.closest('.defect-form-block');
        const visibleBlocks = [...list.querySelectorAll('.defect-form-block')]
            .filter((formBlock) => !formBlock.hidden);
        if (!block || visibleBlocks.length <= 1) {
            return;
        }

        const deleteField = block.querySelector('input[name$="-DELETE"]');
        if (deleteField) {
            deleteField.checked = true;
            block.hidden = true;
            return;
        }

        block.remove();
        reindexForms();
    });
});
