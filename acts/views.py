from django.shortcuts import render


def act_list(request):
    context = {
        'active_page': 'acts',
        'page_title': 'Акты операционного контроля',
        'page_description': 'Раздел будет использоваться для создания, рассмотрения и закрытия актов.',
    }
    return render(request, 'acts/list.html', context)

# Create your views here.
