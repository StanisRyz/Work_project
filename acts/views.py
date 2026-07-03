from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def act_list(request):
    context = {
        'active_page': 'acts',
        'page_title': 'Акты операционного контроля',
        'page_description': 'Раздел будет использоваться для создания, рассмотрения и закрытия актов.',
    }
    return render(request, 'acts/list.html', context)
