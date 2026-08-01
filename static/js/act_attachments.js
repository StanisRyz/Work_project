document.addEventListener('DOMContentLoaded', () => {
    const picker = document.querySelector('.attachment-picker');
    if (!picker) return;

    const input = picker.querySelector('input[type="file"]');
    const trigger = picker.querySelector('[data-attachment-file-trigger]');
    const name = picker.querySelector('[data-attachment-file-name]');
    if (!input || !trigger || !name) return;

    trigger.addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
        name.textContent = input.files?.[0]?.name || 'Выберите файл для загрузки';
        picker.classList.toggle('attachment-picker--selected', Boolean(input.files?.length));
    });
});
